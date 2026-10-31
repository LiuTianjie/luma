import type { ReactNode } from "react";
import { t } from "../i18n";
import type { Lang } from "../types";
import lumaLogoMark from "../assets/luma-logo-mark.png";

// Signed-out screens (sign-in, enrollment links) stand on their own: no workspace
// navigation is shown before the browser holds a session.
export function AuthLayout({ lang, onLangChange, children }: { lang: Lang; onLangChange: (lang: Lang) => void; children: ReactNode }) {
  const zh = lang === "zh";
  return (
    <main id="main" className="relative isolate flex min-h-dvh flex-col items-center justify-center gap-8 overflow-hidden bg-canvas px-4 py-12">
      {/* A faint dot grid that fades toward the edges; decoration only. */}
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:20px_20px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)]" />
      <div className="flex flex-col items-center gap-3">
        <span className="flex size-12 items-center justify-center rounded-xl bg-background shadow-sm ring-1 ring-border">
          <img src={lumaLogoMark} alt="" className="size-7" width={28} height={28} />
        </span>
        <div className="flex items-baseline gap-2">
          <span className="text-xl font-semibold tracking-[-0.02em]">Luma</span>
          <span className="text-sm text-muted-foreground">{t(lang, "title")}</span>
        </div>
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
