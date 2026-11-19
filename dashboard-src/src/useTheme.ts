import { useEffect, useState } from "react";

export type ThemeMode = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const THEME_KEY = "luma.dashboard.theme";
const query = "(prefers-color-scheme: light)";

function readMode(): ThemeMode {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === "light" || stored === "dark" ? stored : "system";
}

function resolve(mode: ThemeMode): ResolvedTheme {
  if (mode === "system") return window.matchMedia(query).matches ? "light" : "dark";
  return mode;
}

function apply(theme: ResolvedTheme, schemeContent: "light" | "dark" | "light dark") {
  document.documentElement.dataset.theme = theme;
  document.documentElement.classList.toggle("dark", theme === "dark");
  const schemeMeta = document.querySelector('meta[name="color-scheme"]');
  if (schemeMeta) schemeMeta.setAttribute("content", schemeContent);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f5f3f3" : "#0a0a0a");
}

/** Three-state theme: follow system (default) / light / dark. Persists explicit
 *  choices only; "system" clears storage and tracks prefers-color-scheme live.
 *  An optional transient override (e.g. Builder SSO handoff) applies without
 *  modifying the user's saved Control preference. */
export function useTheme(override?: ResolvedTheme | null): { mode: ThemeMode; theme: ResolvedTheme; setMode: (mode: ThemeMode) => void } {
  const [mode, setModeState] = useState<ThemeMode>(readMode);
  const [theme, setTheme] = useState<ResolvedTheme>(() => resolve(readMode()));
  const effectiveTheme: ResolvedTheme = override ?? theme;
  const schemeContent = override ?? (mode === "system" ? "light dark" : mode);

  useEffect(() => {
    apply(effectiveTheme, schemeContent);
  }, [effectiveTheme, schemeContent]);

  useEffect(() => {
    if (mode !== "system") return;
    const media = window.matchMedia(query);
    const onChange = () => setTheme(resolve("system"));
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [mode]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_KEY) return;
      const next: ThemeMode = event.newValue === "light" || event.newValue === "dark" ? event.newValue : "system";
      setModeState(next);
      setTheme(resolve(next));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setMode = (next: ThemeMode) => {
    setModeState(next);
    if (next === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, next);
    setTheme(resolve(next));
  };

  return { mode, theme: effectiveTheme, setMode };
}
