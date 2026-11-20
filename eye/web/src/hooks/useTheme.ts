import { useCallback, useEffect, useState } from "react";

export type ThemeMode = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "color-scheme";
const DARK_MEDIA = "(prefers-color-scheme: dark)";

function readStoredMode(): ThemeMode {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw === "light" || raw === "dark" ? raw : "system";
  } catch {
    return "system";
  }
}

function getSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia(DARK_MEDIA).matches ? "dark" : "light";
}

function resolveMode(mode: ThemeMode): ResolvedTheme {
  return mode === "system" ? getSystemTheme() : mode;
}

function applyThemeToDOM(mode: ThemeMode, resolved: ResolvedTheme) {
  const schemeMeta = document.querySelector('meta[name="color-scheme"]');
  if (schemeMeta) {
    schemeMeta.setAttribute("content", mode === "system" ? "light dark" : mode);
  }
  document.documentElement.dataset.theme = resolved;
  document.documentElement.classList.toggle("dark", resolved === "dark");
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) {
    themeMeta.setAttribute("content", resolved === "dark" ? "#121212" : "#f7f7f7");
  }
}

/**
 * Three-state color-scheme hook ("system" | "light" | "dark"):
 * - Updates `<meta name="color-scheme">` ("light dark" for system, or literal "light"/"dark").
 * - Persists explicit overrides in `localStorage("color-scheme")` and removes on "system".
 * - Syncs across tabs via `storage` event and tracks OS `prefers-color-scheme` changes
 *   ONLY while in "system" mode (never clearing an explicit override when OS coincides).
 */
export function useTheme() {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(getSystemTheme);

  const resolved: ResolvedTheme = mode === "system" ? systemTheme : mode;

  useEffect(() => {
    applyThemeToDOM(mode, resolved);
  }, [mode, resolved]);

  useEffect(() => {
    const media = window.matchMedia(DARK_MEDIA);
    const onMediaChange = () => {
      setSystemTheme(media.matches ? "dark" : "light");
    };
    media.addEventListener("change", onMediaChange);

    const onStorage = (ev: StorageEvent) => {
      if (ev.key !== STORAGE_KEY) return;
      const next: ThemeMode = ev.newValue === "light" || ev.newValue === "dark" ? ev.newValue : "system";
      setModeState(next);
    };
    window.addEventListener("storage", onStorage);

    return () => {
      media.removeEventListener("change", onMediaChange);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    try {
      if (next === "system") {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      // Still apply to DOM even if storage is blocked
    }
    applyThemeToDOM(next, resolveMode(next));
  }, []);

  return { mode, theme: resolved, systemTheme, setMode };
}
