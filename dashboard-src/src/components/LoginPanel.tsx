import { FormEvent, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { t } from "../i18n";
import type { Lang } from "../types";
import lumaLogoMark from "../assets/luma-logo-mark.png";

export function LoginPanel({ lang, onSubmit }: { lang: Lang; onSubmit: (token: string) => void }) {
  const [token, setToken] = useState("");
  const fieldId = useId();
  const zh = lang === "zh";

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token.trim()) return;
    onSubmit(token);
  };

  return (
    <Card className="mx-auto w-full max-w-md">
      <CardHeader>
        <div className="flex items-center gap-3">
          <div className="flex size-9 items-center justify-center overflow-hidden rounded-lg bg-muted">
            <img src={lumaLogoMark} alt="" width={24} height={24} />
          </div>
          <div className="flex flex-col gap-1">
            <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{t(lang, "readonly")}</p>
            <CardTitle>{t(lang, "loginTitle")}</CardTitle>
          </div>
        </div>
        <CardDescription>{t(lang, "loginCopy")}</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit}>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor={fieldId}>{zh ? "管理 Token" : "Management token"}</FieldLabel>
              <Input
                id={fieldId}
                autoComplete="current-password"
                name="management-token"
                onChange={(event) => setToken(event.target.value)}
                placeholder={zh ? "luma_…" : "luma_…"}
                spellCheck={false}
                type="password"
                value={token}
              />
            </Field>
            <Button type="submit">{t(lang, "openStatus")}</Button>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  );
}
