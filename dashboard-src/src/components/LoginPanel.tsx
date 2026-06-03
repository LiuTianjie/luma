import { FormEvent, useState } from "react";
import { t } from "../i18n";
import type { Lang } from "../types";

export function LoginPanel({ lang, onSubmit }: { lang: Lang; onSubmit: (token: string) => void }) {
  const [token, setToken] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!token.trim()) return;
    onSubmit(token);
  };

  return (
    <section className="login-panel">
      <div>
        <p className="eyebrow">{t(lang, "readonly")}</p>
        <h1>{t(lang, "loginTitle")}</h1>
        <p>{t(lang, "loginCopy")}</p>
      </div>
      <form onSubmit={submit}>
        <input
          autoComplete="off"
          onChange={(event) => setToken(event.target.value)}
          placeholder="Deploy token"
          spellCheck={false}
          type="password"
          value={token}
        />
        <button type="submit">{t(lang, "openStatus")}</button>
      </form>
    </section>
  );
}
