import { Activity, Boxes, LayoutDashboard, ScrollText, ServerCog, Settings, WandSparkles, type Icon } from "@/components/icons";
import type { NavIconName } from "./navItems";

export const NAV_ICONS: Record<NavIconName, Icon> = {
  overview: LayoutDashboard,
  applications: Boxes,
  deployments: ScrollText,
  observability: Activity,
  infrastructure: ServerCog,
  settings: Settings,
  setup: WandSparkles,
};
