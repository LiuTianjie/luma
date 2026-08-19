import { FormEvent, useId, useState } from "react";
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
    <section className="login-panel">
      <div className="login-panel-brand">
        <div className="brand-mark" aria-hidden="true">
          <img src={lumaLogoMark} alt="" width={24} height={24} />
        </div>
        <div>
          <p className="eyebrow">{t(lang, "readonly")}</p>
          <h1>{t(lang, "loginTitle")}</h1>
        </div>
      </div>
      <p>{t(lang, "loginCopy")}</p>
      <form onSubmit={submit}>
        <label htmlFor={fieldId}>
          <span>{zh ? "管理 Token" : "Management token"}</span>
          <input
            id={fieldId}
            autoComplete="current-password"
            name="management-token"
            onChange={(event) => setToken(event.target.value)}
            placeholder={zh ? "luma_…" : "luma_…"}
            spellCheck={false}
            type="password"
            value={token}
          />
        </label>
        <button type="submit" className="primary">{t(lang, "openStatus")}</button>
      </form>
    </section>
  );
}
