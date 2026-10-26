import type { ReactNode } from "react";
import { t } from "../i18n";
import type { Lang } from "../types";
import lumaLogoMark from "../assets/luma-logo-mark.png";

// Signed-out screens (sign-in, enrollment links) stand on their own: no workspace
// navigation is shown before the browser holds a session.
export function AuthLayout({ lang, onLangChange, children }: { lang: Lang; onLangChange: (lang: Lang) => void; children: ReactNode }) {
  const zh = lang === "zh";
  return (
    <main id="main" className="flex min-h-dvh flex-col items-center justify-center gap-8 bg-sidebar px-4 py-12">
      <div className="flex items-center gap-2.5">
        <img src={lumaLogoMark} alt="" className="size-8" width={32} height={32} />
        <span className="text-xl font-semibold tracking-tight">Luma</span>
        <span className="text-sm text-muted-foreground">{t(lang, "title")}</span>
      </div>
      <div className="w-full max-w-sm">{children}</div>
      <footer className="flex items-center gap-2 text-xs text-muted-foreground">
        <span translate="no">{window.location.host}</span>
        <span aria-hidden="true">·</span>
        <button
          type="button"
          className="rounded-sm underline-offset-4 hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          lang={zh ? "en" : "zh-CN"}
          onClick={() => onLangChange(zh ? "en" : "zh")}
        >
          {zh ? "English" : "中文"}
        </button>
      </footer>
    </main>
  );
}
