import { type FormEvent, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { t } from "../i18n";
import type { Lang } from "../types";
import lumaLogoMark from "../assets/luma-logo-mark.png";

export function LoginPanel({ lang, onSubmit }: { lang: Lang; onSubmit: (token: string) => void }) {
  const [token, setToken] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();
  const zh = lang === "zh";
  const invalid = submitted && !token.trim();

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitted(true);
    if (!token.trim()) {
      inputRef.current?.focus();
      return;
    }
    onSubmit(token);
  };

  return (
    <Card className="mx-auto w-full max-w-md" aria-labelledby={`${fieldId}-title`}>
      <CardHeader>
        <CardDescription>{t(lang, "readonly")}</CardDescription>
        <CardTitle id={`${fieldId}-title`} role="heading" aria-level={1}>{t(lang, "loginTitle")}</CardTitle>
        <CardAction><img src={lumaLogoMark} alt="Luma" className="size-9" width={36} height={36} /></CardAction>
      </CardHeader>
      <CardContent>
        <form noValidate onSubmit={submit}>
          <FieldGroup>
            <Field data-invalid={invalid}>
              <FieldLabel htmlFor={fieldId}>{zh ? "管理 Token" : "Management token"}</FieldLabel>
              <Input
                ref={inputRef}
                id={fieldId}
                aria-describedby={invalid ? `${fieldId}-description ${fieldId}-error` : `${fieldId}-description`}
                aria-invalid={invalid}
                autoComplete="current-password"
                name="management-token"
                onChange={(event) => setToken(event.target.value)}
                placeholder="luma_…"
                required
                spellCheck={false}
                type="password"
                value={token}
              />
              <FieldDescription id={`${fieldId}-description`}>{t(lang, "loginCopy")}</FieldDescription>
              {invalid ? <FieldError id={`${fieldId}-error`}>{zh ? "请输入管理 Token。" : "Enter a management token."}</FieldError> : null}
            </Field>
            <Field>
              <Button type="submit">{t(lang, "openStatus")}</Button>
            </Field>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  );
}
