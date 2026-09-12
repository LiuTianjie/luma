import { useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw } from "lucide-react";
import type { Lang } from "../types";
import { fetchSecrets, setSecret } from "../controlResourcesApi";
import { fetchDeploymentConfig } from "../deploymentConfigApi";
import {
  applicationSecretRows,
  applicationSecretScope,
} from "../applicationSecrets";
import { useRouter } from "../router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardAction,
  CardFooter,
} from "@/components/ui/card";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Field,
  FieldLabel,
  FieldGroup,
  FieldDescription,
  FieldError,
} from "@/components/ui/field";
import {
  Empty,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
  EmptyDescription,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";

export function ApplicationSecrets({
  app,
  token,
  lang,
  onUpdate,
}: {
  app: string;
  token: string;
  lang: Lang;
  onUpdate: () => void;
}) {
  const zh = lang === "zh";
  const { navigate } = useRouter();
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<{
    scope: string;
    names: string[];
    texts: string[];
    configError: boolean;
  } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editor, setEditor] = useState<{ name: string; fixed: boolean } | null>(
    null,
  );
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [writeError, setWriteError] = useState("");
  useEffect(() => {
    setEditor(null);
    setValue("");
    setWriteError("");
    setNotice("");
  }, [token]);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setData(null);
    setError("");
    void Promise.allSettled([
      fetchSecrets({ token, signal: controller.signal }),
      fetchDeploymentConfig({ token, name: app }),
    ]).then(([secrets, config]) => {
      if (!active) return;
      if (secrets.status === "rejected") {
        setError(
          String(
            secrets.reason instanceof Error
              ? secrets.reason.message
              : secrets.reason,
          ),
        );
        return;
      }
      const resolved = config.status === "fulfilled" ? config.value : null;
      setData({
        scope: resolved?.slug || applicationSecretScope(app),
        names: secrets.value.secrets || [],
        texts: [resolved?.manifest || "", resolved?.composeContent || ""],
        configError: !resolved,
      });
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [app, token, revision]);
  const rows = data
    ? applicationSecretRows(data.names, data.scope, data.texts)
    : [];
  const nameInvalid = Boolean(
    editor?.name.trim() && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(editor.name.trim()),
  );
  const openEditor = (name = "", fixed = false) => {
    setEditor({ name, fixed });
    setValue("");
    setWriteError("");
  };
  const closeEditor = () => {
    if (!busy) {
      setEditor(null);
      setValue("");
      setWriteError("");
    }
  };
  const save = async () => {
    if (
      !data?.scope ||
      !editor ||
      !/^[A-Za-z_][A-Za-z0-9_]*$/.test(editor.name.trim()) ||
      !value ||
      busy
    )
      return;
    setBusy(true);
    setWriteError("");
    try {
      await setSecret({
        token,
        scope: data.scope,
        name: editor.name.trim(),
        value,
      });
      setValue("");
      setEditor(null);
      setNotice(
        zh
          ? "密钥已保存。重新部署应用后，新值才会进入运行实例；已有实例不会自动更新。"
          : "Secret saved. Redeploy the application to apply it; existing instances are not updated automatically.",
      );
      setRevision((current) => current + 1);
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      {notice ? (
        <Alert>
          <AlertTitle>
            {zh ? "待应用新密钥" : "Redeployment required"}
          </AlertTitle>
          <AlertDescription>
            {notice}
            <Button
              className="mt-3 w-fit"
              size="sm"
              variant="outline"
              onClick={onUpdate}
            >
              {zh ? "更新应用" : "Update application"}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>{zh ? "应用密钥" : "Application secrets"}</CardTitle>
          <CardDescription>
            {zh
              ? "敏感值只写不回显。新增和轮转仅保存到当前应用作用域。"
              : "Values are write-only. New and rotated secrets are saved to this application's scope."}
          </CardDescription>
          <CardAction>
            <Button
              size="sm"
              disabled={!data?.scope}
              onClick={() => openEditor()}
            >
              <Plus data-icon="inline-start" />
              {zh ? "新增密钥" : "Add secret"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>
                {zh ? "读取失败" : "Could not load secrets"}
              </AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
          {data?.configError ? (
            <Alert>
              <AlertDescription>
                {zh
                  ? "部署配置读取失败，仅展示已保存的应用密钥，暂时无法识别配置中的全局引用。"
                  : "Deployment configuration is unavailable. Only stored application secrets are shown; global references cannot be identified."}
              </AlertDescription>
            </Alert>
          ) : null}
          {data ? (
            <>
              <p className="text-xs text-muted-foreground wrap-anywhere">
                {zh ? "作用域" : "Scope"}：<code>{data.scope}</code>
              </p>
              <Table
                containerProps={{
                  tabIndex: 0,
                  role: "region",
                  "aria-label": zh
                    ? "应用密钥，可横向滚动"
                    : "Application secrets, horizontally scrollable",
                }}
              >
                <TableHeader>
                  <TableRow>
                    <TableHead>{zh ? "名称" : "Name"}</TableHead>
                    <TableHead>{zh ? "来源" : "Source"}</TableHead>
                    <TableHead>
                      {zh ? "配置引用" : "Configuration reference"}
                    </TableHead>
                    <TableHead className="text-right">
                      {zh ? "操作" : "Actions"}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.name}>
                      <TableCell>
                        <code>{row.name}</code>
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            row.source === "missing" ? "warning" : "secondary"
                          }
                        >
                          {row.source === "application"
                            ? zh
                              ? "应用专用"
                              : "Application"
                            : row.source === "global"
                              ? zh
                                ? "全局共享"
                                : "Global"
                              : zh
                                ? "未保存"
                                : "Not stored"}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {row.referenced ? (
                          <code>{`\${${row.name}}`}</code>
                        ) : zh ? (
                          "未在配置中发现"
                        ) : (
                          "Not found in configuration"
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          {row.source === "global" ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() =>
                                navigate(
                                  `/settings/secrets/new?name=${encodeURIComponent(row.name)}`,
                                )
                              }
                            >
                              {zh ? "管理全局值" : "Manage global value"}
                            </Button>
                          ) : null}
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => openEditor(row.name, true)}
                          >
                            {row.source === "application"
                              ? zh
                                ? "轮转"
                                : "Rotate"
                              : row.source === "global"
                                ? zh
                                  ? "设置应用专用值"
                                  : "Set application override"
                                : zh
                                  ? "设置密钥"
                                  : "Set secret"}
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                  {!rows.length ? (
                    <TableRow>
                      <TableCell colSpan={4} className="whitespace-normal">
                        <Empty>
                          <EmptyHeader>
                            <EmptyMedia variant="icon">
                              <KeyRound />
                            </EmptyMedia>
                            <EmptyTitle>
                              {zh ? "暂无应用密钥" : "No application secrets"}
                            </EmptyTitle>
                            <EmptyDescription>
                              {zh
                                ? "可新增密钥，并在部署配置中用 ${NAME} 引用。"
                                : "Add one and reference it as ${NAME} in deployment configuration."}
                            </EmptyDescription>
                          </EmptyHeader>
                        </Empty>
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </>
          ) : !error ? (
            <div
              className="flex flex-col gap-3"
              role="status"
              aria-label={zh ? "正在读取密钥…" : "Loading secrets…"}
            >
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-32 w-full" />
              <span className="sr-only">
                {zh ? "正在读取密钥…" : "Loading secrets…"}
              </span>
            </div>
          ) : null}
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setRevision((current) => current + 1)}
          >
            <RefreshCw data-icon="inline-start" />
            {zh ? "刷新" : "Refresh"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate("/settings/secrets")}
          >
            {zh ? "全部密钥" : "All secrets"}
          </Button>
        </CardFooter>
      </Card>
      <Dialog
        open={!!editor}
        onOpenChange={(open) => {
          if (!open) closeEditor();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {zh ? "保存应用密钥" : "Save application secret"}
            </DialogTitle>
            <DialogDescription>
              {zh
                ? `保存到 ${data?.scope || app}，不会修改同名全局值。保存后需重新部署应用。`
                : `Save to ${data?.scope || app} without changing the global value. Redeploy the application afterward.`}
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <FieldGroup>
              <Field
                data-disabled={busy || editor?.fixed}
                data-invalid={nameInvalid}
              >
                <FieldLabel htmlFor="application-secret-name">
                  {zh ? "密钥名称" : "Secret name"}
                </FieldLabel>
                <Input
                  id="application-secret-name"
                  value={editor?.name || ""}
                  disabled={busy || editor?.fixed}
                  aria-invalid={nameInvalid}
                  aria-describedby={
                    nameInvalid ? "application-secret-name-error" : undefined
                  }
                  required
                  pattern="[A-Za-z_][A-Za-z0-9_]*"
                  onChange={(event) =>
                    setEditor((current) =>
                      current ? { ...current, name: event.target.value } : null,
                    )
                  }
                />
                {nameInvalid ? (
                  <FieldError id="application-secret-name-error">
                    {zh
                      ? "使用字母、数字或下划线，且不能以数字开头。"
                      : "Use letters, numbers or underscores, starting with a letter or underscore."}
                  </FieldError>
                ) : null}
              </Field>
              <Field data-disabled={busy}>
                <FieldLabel htmlFor="application-secret-value">
                  {zh ? "新值" : "New value"}
                </FieldLabel>
                <Input
                  id="application-secret-value"
                  type="password"
                  autoComplete="new-password"
                  value={value}
                  disabled={busy}
                  required
                  aria-describedby="application-secret-value-description"
                  onChange={(event) => setValue(event.target.value)}
                />
                <FieldDescription id="application-secret-value-description">
                  {zh
                    ? "保存后不再显示密钥值。"
                    : "The secret value is not shown after saving."}
                </FieldDescription>
              </Field>
              {writeError ? (
                <Alert variant="destructive">
                  <AlertTitle>
                    {zh ? "保存失败" : "Could not save secret"}
                  </AlertTitle>
                  <AlertDescription>{writeError}</AlertDescription>
                </Alert>
              ) : null}
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy}
                  onClick={closeEditor}
                >
                  {zh ? "取消" : "Cancel"}
                </Button>
                <Button
                  type="submit"
                  disabled={
                    busy || nameInvalid || !value || !editor?.name.trim()
                  }
                >
                  {busy ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                  {busy ? (zh ? "保存中…" : "Saving…") : zh ? "保存" : "Save"}
                </Button>
              </DialogFooter>
            </FieldGroup>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
