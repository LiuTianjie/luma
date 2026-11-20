import { useCallback, useEffect, useState } from "react";
import { sessionCredential, UNAUTHORIZED_EVENT } from "./apiClient";
import {
  exchangeToken,
  fetchAuthConfig,
  fetchCurrentSession,
  signInWithPasskey,
  signOutSession,
  SignInUnavailable,
  type AuthConfig,
} from "./authApi";
import { useDashboardQueryClient } from "./queryClient";

// Earlier dashboards kept the management token here. It is traded for a session
// once and removed; nothing is written back.
const LEGACY_TOKEN_KEY = "luma.dashboard.deployToken";
const isDev = typeof window !== "undefined" && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");

export type AuthStatus = "checking" | "signedOut" | "signedIn";

export function useDashboardAuth() {
  const queryClient = useDashboardQueryClient();
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [expired, setExpired] = useState(false);

  const refreshConfig = useCallback(async () => {
    try {
      setConfig(await fetchAuthConfig());
    } catch {
      setConfig(null);
    }
  }, []);

  const adoptSession = useCallback(() => {
    setToken(sessionCredential());
    setExpired(false);
    setStatus("signedIn");
  }, []);

  const signedOut = useCallback(() => {
    queryClient.clear();
    setToken("");
    setStatus("signedOut");
    void refreshConfig();
  }, [queryClient, refreshConfig]);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        if (await fetchCurrentSession()) {
          if (active) adoptSession();
          return;
        }
        const legacy = localStorage.getItem(LEGACY_TOKEN_KEY);
        if (legacy) {
          localStorage.removeItem(LEGACY_TOKEN_KEY);
          try {
            await exchangeToken(legacy);
            if (active) adoptSession();
            return;
          } catch {
            // Fall through to the sign-in screen; the token is not kept.
          }
        }
      } catch (error) {
        // The Vite dev server mocks the API with a fixed Bearer token.
        if (isDev && error instanceof SignInUnavailable) {
          if (active) {
            setToken("dev-token");
            setStatus("signedIn");
          }
          return;
        }
      }
      if (active) signedOut();
    })();
    return () => { active = false; };
  }, [adoptSession, signedOut]);

  const signInWithToken = useCallback(async (raw: string) => {
    try {
      await exchangeToken(raw);
    } catch (error) {
      if (isDev && /HTTP 404/.test(String(error))) {
        setToken(raw.trim());
        setStatus("signedIn");
        return;
      }
      throw error;
    }
    adoptSession();
  }, [adoptSession]);

  const signInPasskey = useCallback(async () => {
    await signInWithPasskey();
    adoptSession();
  }, [adoptSession]);

  const signOut = useCallback(async () => {
    try {
      await signOutSession();
    } catch {
      // The cookie may already be gone; the browser forgets the session either way.
    }
    setExpired(false);
    signedOut();
  }, [signedOut]);

  // Control rejected the session (expired, revoked or its passkey removed).
  const expire = useCallback(() => {
    setExpired(true);
    signedOut();
  }, [signedOut]);

  useEffect(() => {
    if (status !== "signedIn") return;
    const onUnauthorized = () => expire();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [status, expire]);

  return { token, status, config, expired, signInWithToken, signInPasskey, adoptSession, signOut, expire };
}
