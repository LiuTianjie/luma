import { LogOut, Monitor, Moon, RefreshCw, Settings2, Sun } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";
import type { ThemeMode } from "../useTheme";

type Props = {
  clusterId: string;
  lang: Lang;
  lastUpdated: Date | null;
  syncStatus: SyncStatus;
  themeMode: ThemeMode;
  onLangChange: (lang: Lang) => void;
  onThemeModeChange: (mode: ThemeMode) => void;
  onRefresh: () => void;
  onSignOut: () => void;
  compact?: boolean;
};

export function Topbar({
  clusterId,
  lang,
  lastUpdated,
  syncStatus,
  themeMode,
  onLangChange,
  onThemeModeChange,
  onRefresh,
  onSignOut,
  compact = false,
}: Props) {
  const timeFormatter = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const statusText = syncStatus === "updated" && lastUpdated
    ? `${t(lang, "updated")} ${timeFormatter.format(lastUpdated)}`
    : t(lang, syncStatus);
  const themeOptions: Array<{ mode: ThemeMode; icon: typeof Sun; label: string }> = [
    { mode: "system", icon: Monitor, label: lang === "zh" ? "跟随系统" : "Follow system" },
    { mode: "light", icon: Sun, label: lang === "zh" ? "日间模式" : "Light mode" },
    { mode: "dark", icon: Moon, label: lang === "zh" ? "夜间模式" : "Dark mode" },
  ];

  if (compact) {
    return (
      <header className="flex h-10 shrink-0 items-center px-2">
        <SidebarTrigger />
      </header>
    );
  }

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
      <SidebarTrigger />
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-xs text-muted-foreground">{t(lang, "cluster")}</span>
        <Badge variant="outline" className="max-w-56 truncate font-mono" translate="no">{clusterId}</Badge>
      </div>
      <div className="ml-auto flex items-center gap-1.5">
        <span className="hidden text-xs text-muted-foreground sm:inline" title={statusText} role="status">
          {statusText}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="outline" size="sm" />}>
            <Settings2 data-icon="inline-start" />
            <span className="hidden sm:inline">{lang === "zh" ? "偏好" : "Display"}</span>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-52">
            <DropdownMenuGroup>
              <DropdownMenuLabel>{lang === "zh" ? "外观" : "Appearance"}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={themeMode} onValueChange={(value) => onThemeModeChange(value as ThemeMode)}>
                {themeOptions.map(({ mode, icon: Icon, label }) => (
                  <DropdownMenuRadioItem key={mode} value={mode}>
                    <Icon />
                    {label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel>{lang === "zh" ? "语言" : "Language"}</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={lang} onValueChange={(value) => onLangChange(value as Lang)}>
                <DropdownMenuRadioItem value="zh">中文</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="en">English</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        <Button variant="outline" size="sm" onClick={onRefresh} aria-label={t(lang, "refresh")} title={t(lang, "refresh")}>
          <RefreshCw data-icon="inline-start" />
          <span className="hidden sm:inline">{t(lang, "refresh")}</span>
        </Button>
        <Button variant="ghost" size="sm" onClick={onSignOut} aria-label={t(lang, "signOut")} title={t(lang, "signOut")}>
          <LogOut data-icon="inline-start" />
          <span className="hidden sm:inline">{t(lang, "signOut")}</span>
        </Button>
      </div>
    </header>
  );
}
