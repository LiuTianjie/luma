import type { Lang } from "../types";

export function NotFound({ lang, onHome }: { lang: Lang; onHome: () => void }) {
  const zh = lang === "zh";
  return (
    <section className="empty-state">
      <p>{zh ? "页面不存在" : "Page not found"}</p>
      <button type="button" className="ghost text-link-button" onClick={onHome}>
        {zh ? "返回总览" : "Back to overview"}
      </button>
    </section>
  );
}
