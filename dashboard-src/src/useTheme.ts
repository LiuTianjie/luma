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

function apply(theme: ResolvedTheme) {
  document.documentElement.dataset.theme = theme;
}

/** Three-state theme: follow system (default) / light / dark. Persists explicit
 *  choices only; "system" clears storage and tracks prefers-color-scheme live. */
export function useTheme(): { mode: ThemeMode; theme: ResolvedTheme; setMode: (mode: ThemeMode) => void } {
  const [mode, setModeState] = useState<ThemeMode>(readMode);
  const [theme, setTheme] = useState<ResolvedTheme>(() => resolve(readMode()));

  useEffect(() => {
    apply(theme);
  }, [theme]);

  useEffect(() => {
    if (mode !== "system") return;
    const media = window.matchMedia(query);
    const onChange = () => setTheme(resolve("system"));
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [mode]);

  const setMode = (next: ThemeMode) => {
    setModeState(next);
    if (next === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, next);
    setTheme(resolve(next));
  };

  return { mode, theme, setMode };
}
