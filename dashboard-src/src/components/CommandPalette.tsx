import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import { Boxes, CornerDownLeft, Search, Server, type Icon } from "@/components/icons";
import { cn } from "@/lib/utils";
import { NAV_ICONS } from "../navIcons";
import type { NavGroup } from "../navItems";
import { ROUTE_BY_PAGE } from "../routes";
import type { Lang } from "../types";

export type PaletteEntry = {
  id: string;
  group: string;
  label: string;
  hint?: string;
  href: string;
  icon: Icon;
  keywords?: string;
};

/** Destinations from the sidebar plus live objects, so ⌘K reaches anything in one step. */
export function buildPaletteEntries(lang: Lang, groups: NavGroup[], applications: string[], nodes: string[]): PaletteEntry[] {
  const zh = lang === "zh";
  const pages: PaletteEntry[] = [];
  for (const group of groups) {
    for (const item of group.items) {
      const icon = NAV_ICONS[item.icon];
      if (!item.children?.length) {
        pages.push({ id: `page:${item.id}`, group: zh ? "页面" : "Pages", label: item.label, hint: item.detail, href: ROUTE_BY_PAGE[item.id], icon });
        continue;
      }
      for (const child of item.children) {
        pages.push({ id: `page:${child.href}`, group: zh ? "页面" : "Pages", label: child.label, hint: item.label, href: child.href, icon, keywords: child.detail });
      }
    }
  }
  pages.push({ id: "page:/create", group: zh ? "页面" : "Pages", label: zh ? "创建应用" : "Create application", hint: zh ? "应用" : "Applications", href: "/create", icon: NAV_ICONS.applications });
  pages.push({ id: "page:/builds", group: zh ? "页面" : "Pages", label: zh ? "从 Git 构建" : "Build from Git", hint: zh ? "应用" : "Applications", href: "/builds", icon: NAV_ICONS.applications });
  pages.push({ id: "page:/fleet/join", group: zh ? "页面" : "Pages", label: zh ? "加入节点" : "Join a node", hint: zh ? "基础设施" : "Infrastructure", href: "/fleet/join", icon: NAV_ICONS.infrastructure });
  const apps = applications.map((stack) => ({ id: `app:${stack}`, group: zh ? "应用" : "Applications", label: stack, href: `/apps/${encodeURIComponent(stack)}/overview`, icon: Boxes }));
  const fleet = nodes.map((name) => ({ id: `node:${name}`, group: zh ? "节点" : "Nodes", label: name, href: `/fleet/nodes/${encodeURIComponent(name)}`, icon: Server }));
  return [...pages, ...apps, ...fleet];
}

function score(entry: PaletteEntry, query: string) {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const label = entry.label.toLowerCase();
  if (label === q) return 100;
  if (label.startsWith(q)) return 80;
  if (label.includes(q)) return 60;
  const rest = `${entry.hint || ""} ${entry.keywords || ""} ${entry.group}`.toLowerCase();
  return rest.includes(q) ? 30 : 0;
}

export function CommandPalette({ lang, open, onOpenChange, entries, onSelect }: {
  lang: Lang;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entries: PaletteEntry[];
  onSelect: (href: string) => void;
}) {
  const zh = lang === "zh";
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const results = useMemo(() => entries
    .map((entry, index) => ({ entry, index, score: score(entry, query) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, 40)
    .map((item) => item.entry), [entries, query]);
  const grouped = useMemo(() => {
    const map = new Map<string, PaletteEntry[]>();
    for (const entry of results) map.set(entry.group, [...(map.get(entry.group) || []), entry]);
    return [...map.entries()];
  }, [results]);
  const ordered = grouped.flatMap(([, items]) => items);

  useEffect(() => { if (open) { setQuery(""); setActive(0); } }, [open]);
  useEffect(() => { setActive(0); }, [query]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const choose = (entry: PaletteEntry | undefined) => {
    if (!entry) return;
    onOpenChange(false);
    onSelect(entry.href);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((value) => Math.min(ordered.length - 1, value + 1)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((value) => Math.max(0, value - 1)); }
    else if (event.key === "Enter") { event.preventDefault(); choose(ordered[active]); }
  };
  const activeId = ordered[active] ? `palette-${ordered[active].id}` : undefined;
  let index = -1;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/30 duration-150 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0 dark:bg-black/55" />
        <DialogPrimitive.Popup
          aria-label={zh ? "搜索或跳转" : "Search or jump to"}
          className="fixed top-[12vh] left-1/2 z-50 flex max-h-[min(32rem,76vh)] w-[min(40rem,calc(100%-2rem))] -translate-x-1/2 flex-col overflow-hidden rounded-xl bg-popover text-popover-foreground shadow-[0_24px_48px_-12px_oklch(0_0_0/0.3),0_4px_12px_-4px_var(--shadow-color)] ring-1 ring-border outline-none duration-150 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-[0.98] data-closed:animate-out data-closed:fade-out-0"
        >
          <div className="flex items-center gap-2.5 border-b border-hairline px-4">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input
              autoFocus
              role="combobox"
              aria-expanded="true"
              aria-controls="command-palette-list"
              aria-activedescendant={activeId}
              aria-autocomplete="list"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={zh ? "搜索页面、应用或节点…" : "Search pages, applications or nodes…"}
              className="h-12 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-subtle-foreground"
            />
            <kbd className="rounded border border-border bg-muted px-1.5 font-sans text-[0.6875rem] text-muted-foreground">Esc</kbd>
          </div>
          <div ref={listRef} id="command-palette-list" role="listbox" aria-label={zh ? "结果" : "Results"} className="min-h-0 flex-1 overflow-y-auto p-1.5">
            {grouped.length ? grouped.map(([group, items]) => (
              <div key={group} role="group" aria-label={group} className="py-1">
                <div className="px-2.5 pt-1.5 pb-1 text-[0.6875rem] font-medium tracking-wide text-subtle-foreground uppercase">{group}</div>
                {items.map((entry) => {
                  index += 1;
                  const current = index;
                  const Icon = entry.icon;
                  return (
                    <div
                      key={entry.id}
                      id={`palette-${entry.id}`}
                      role="option"
                      aria-selected={current === active}
                      data-index={current}
                      onMouseMove={() => setActive(current)}
                      onClick={() => choose(entry)}
                      className={cn("flex h-9 cursor-pointer items-center gap-2.5 rounded-md px-2.5 text-[0.8125rem]", current === active ? "bg-accent text-accent-foreground" : "text-foreground")}
                    >
                      <Icon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="truncate font-medium">{entry.label}</span>
                      {entry.hint ? <span className="truncate text-muted-foreground">{entry.hint}</span> : null}
                      {current === active ? <CornerDownLeft className="ml-auto size-3.5 shrink-0 text-muted-foreground" /> : null}
                    </div>
                  );
                })}
              </div>
            )) : (
              <p className="px-3 py-10 text-center text-sm text-muted-foreground">{zh ? "没有匹配的结果" : "No matching results"}</p>
            )}
          </div>
          <div className="flex items-center gap-4 border-t border-hairline bg-muted/40 px-4 py-2 text-[0.6875rem] text-muted-foreground">
            <span className="flex items-center gap-1"><kbd className="rounded border border-border bg-background px-1 font-sans">↑</kbd><kbd className="rounded border border-border bg-background px-1 font-sans">↓</kbd>{zh ? "选择" : "Navigate"}</span>
            <span className="flex items-center gap-1"><kbd className="rounded border border-border bg-background px-1 font-sans">↵</kbd>{zh ? "打开" : "Open"}</span>
          </div>
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
