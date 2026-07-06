import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

// Minimal History-API router for the dashboard SPA.
//
// The app is served under the /dashboard/ base (Vite `base`, Control server route),
// so `basename` is stripped from window.location.pathname to yield app-relative paths
// like "/apps/foo". Clean URLs (pushState) give shareable, refresh-safe deep links; the
// Control server's index.html fallback (see luma/control/server.py _dashboard_asset)
// makes a hard refresh at any /dashboard/* route load the SPA. Kept dependency-free and
// intentionally small — this project runs lean and does not need a full router package.

const BASENAME = "/dashboard";

function stripBase(pathname: string): string {
  if (pathname === BASENAME || pathname === BASENAME + "/") return "/";
  if (pathname.startsWith(BASENAME + "/")) return pathname.slice(BASENAME.length) || "/";
  return pathname || "/";
}

function toHref(path: string): string {
  const clean = path.startsWith("/") ? path : `/${path}`;
  return clean === "/" ? `${BASENAME}/` : `${BASENAME}${clean}`;
}

type RouterValue = {
  path: string; // app-relative pathname, e.g. "/apps/foo"
  search: string; // raw search string including the leading "?", or ""
  navigate: (to: string, opts?: { replace?: boolean }) => void;
};

const RouterContext = createContext<RouterValue | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(() => ({
    path: stripBase(window.location.pathname),
    search: window.location.search,
  }));

  useEffect(() => {
    const onPop = () => setLocation({ path: stripBase(window.location.pathname), search: window.location.search });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: string, opts?: { replace?: boolean }) => {
    // Split only on the first "?" so query values that themselves contain "?" survive.
    const queryIndex = to.indexOf("?");
    const pathPart = queryIndex === -1 ? to : to.slice(0, queryIndex);
    const searchPart = queryIndex === -1 ? "" : to.slice(queryIndex + 1);
    const nextPath = pathPart.startsWith("/") ? pathPart : `/${pathPart}`;
    const search = searchPart ? `?${searchPart}` : "";
    const href = toHref(nextPath) + search;
    if (opts?.replace) window.history.replaceState({}, "", href);
    else window.history.pushState({}, "", href);
    setLocation({ path: nextPath, search });
  }, []);

  const value = useMemo<RouterValue>(() => ({ ...location, navigate }), [location, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter(): RouterValue {
  const value = useContext(RouterContext);
  if (!value) throw new Error("useRouter must be used within a RouterProvider");
  return value;
}

// Parse the current search string into a plain object.
export function useSearchParams(): URLSearchParams {
  const { search } = useRouter();
  return useMemo(() => new URLSearchParams(search), [search]);
}
