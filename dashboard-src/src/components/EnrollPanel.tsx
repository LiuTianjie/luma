import { type FormEvent, useId, useState } from "react";
import { FingerprintPattern, TriangleAlert } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { passkeysSupported, registerPasskey } from "../authApi";
import type { Lang } from "../types";

// The one-time code travels in the URL fragment, which browsers never send to
// Control or include in Referer headers.
export function enrollmentCodeFromHash(hash: string): string {
  return new URLSearchParams(hash.replace(/^#/, "")).get("code") || "";
}

// Adds the first (or another) passkey from a link printed by `luma passkey enroll`
// or created in Settings. Finishing it signs this browser in.
export function EnrollPanel({ lang, onEnrolled }: { lang: Lang; onEnrolled: () => void }) {
  const [code] = useState(() => enrollmentCodeFromHash(window.location.hash));
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fieldId = useId();
  const zh = lang === "zh";
  const supported = passkeysSupported();

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await registerPasskey({ enrollmentCode: code, name });
      window.history.replaceState(window.history.state, "", window.location.pathname);
      onEnrolled();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card aria-labelledby={`${fieldId}-title`}>
      <CardHeader>
        <CardTitle id={`${fieldId}-title`} role="heading" aria-level={1} className="text-lg">{zh ? "添加 Passkey" : "Add a passkey"}</CardTitle>
        <CardDescription>{zh ? "为这台设备创建 Passkey，之后用它登录控制台。" : "Create a passkey on this device and use it to sign in to the console."}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!code || !supported ? (
          <Alert variant="destructive">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>{!code ? (zh ? "链接不完整" : "Incomplete link") : (zh ? "浏览器不支持 Passkey" : "Passkeys are not supported here")}</AlertTitle>
            <AlertDescription>
              {!code
                ? (zh ? "请打开 luma passkey enroll 打印的完整链接，包括 # 之后的部分。" : "Open the full link printed by luma passkey enroll, including the part after #.")
                : (zh ? "请换用支持 Passkey 的浏览器，并通过 Control 域名的 HTTPS 地址打开。" : "Use a browser with passkey support and open the link on the Control domain over HTTPS.")}
            </AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive" aria-live="assertive">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>{zh ? "未能添加 Passkey" : "Could not add the passkey"}</AlertTitle>
            <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
          </Alert>
        ) : null}
        <form onSubmit={(event) => void submit(event)}>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor={fieldId}>{zh ? "名称" : "Name"}</FieldLabel>
              <Input id={fieldId} className="h-9" value={name} maxLength={64} placeholder={zh ? "例如 MacBook Touch ID" : "For example MacBook Touch ID"} onChange={(event) => setName(event.target.value)} />
              <FieldDescription>{zh ? "链接只能使用一次，15 分钟后失效。完成后这个浏览器会直接登录。" : "The link works once and expires after 15 minutes. This browser is signed in when you finish."}</FieldDescription>
            </Field>
            <Field>
              <Button type="submit" size="lg" className="w-full" disabled={busy || !code || !supported}>
                {busy ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <FingerprintPattern data-icon="inline-start" />}
                {zh ? "创建 Passkey" : "Create passkey"}
              </Button>
            </Field>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  );
}
