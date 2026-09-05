import type { Lang } from "../types";
import { toHref, useRouter } from "../router";

/** Shared object navigation; every destination remains a refreshable URL. */
export function InfrastructureNavigation({ lang }: { lang: Lang }) {
  const { path, navigate } = useRouter();
  const zh = lang === "zh";
  const items = [
    { href: "/fleet", label: zh ? "节点" : "Nodes", active: path.startsWith("/fleet") && !path.startsWith("/fleet/network") },
    { href: "/storage", label: zh ? "存储" : "Storage", active: path.startsWith("/storage") },
    { href: "/registry", label: zh ? "镜像" : "Registry", active: path.startsWith("/registry") },
    { href: "/fleet/network", label: zh ? "网络" : "Network", active: path.startsWith("/fleet/network") },
  ];
  return <nav className="workspace-tabs infrastructure-navigation" aria-label={zh ? "基础设施" : "Infrastructure"}>
    {items.map(({ href, label, active }) => <a key={href} href={toHref(href)} aria-current={active ? "page" : undefined} className={active ? "active" : ""} onClick={(event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); navigate(href);
    }}>{label}</a>)}
  </nav>;
}
