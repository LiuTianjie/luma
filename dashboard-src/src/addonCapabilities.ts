export type AddonSummaryItem = {
  id: string;
  name: string;
  installed: boolean;
  status: "running" | "configured" | "not_installed" | string;
  consoleUrl: string;
  capabilities: string[];
};

export type DashboardAddonsSummary = {
  known: boolean;
  active: string[];
  capabilities: string[];
  urls: Record<string, string>;
  items?: Record<string, AddonSummaryItem>;
};

export type AddonCapability =
  | "builder.git_import"
  | "builder.runs"
  | "eye.rum"
  | "observe.status"
  | "observe.apps"
  | "observe.logs"
  | "observe.traces"
  | "observe.dashboards"
  | "observe.alerts"
  | "observe.settings";

/**
 * Check whether an optional addon UI capability is active.
 *
 * When talking to an older Control server (where `addons.known` is absent),
 * core-existing capabilities default to `true` for backward compatibility,
 * while net-new extension slots (like `eye.rum`) stay off unless explicitly known.
 */
export function hasAddonCapability(
  addons: DashboardAddonsSummary | null | undefined,
  capability: string | undefined,
  options?: { defaultWhenUnknown?: boolean },
): boolean {
  if (!capability) return true;
  if (!addons?.known) {
    if (options?.defaultWhenUnknown !== undefined) return options.defaultWhenUnknown;
    return !capability.startsWith("eye.");
  }
  return addons.capabilities.includes(capability);
}

export function hasAnyAddonCapability(
  addons: DashboardAddonsSummary | null | undefined,
  capabilities: readonly string[],
): boolean {
  return capabilities.some((cap) => hasAddonCapability(addons, cap));
}

export function isAddonActive(
  addons: DashboardAddonsSummary | null | undefined,
  addonId: string,
): boolean {
  if (!addons?.known) return addonId !== "eye";
  return addons.active.includes(addonId);
}

export function addonConsoleUrl(
  addons: DashboardAddonsSummary | null | undefined,
  addonId: string,
): string {
  return addons?.urls?.[addonId] || addons?.items?.[addonId]?.consoleUrl || "";
}
