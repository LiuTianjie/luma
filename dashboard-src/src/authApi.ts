// Dashboard sign-in: browser sessions, passkeys (WebAuthn) and sign-in policy.
//
// The browser never keeps the management token. Signing in, with a passkey or by
// pasting the token once, makes Control set an HttpOnly session cookie; every
// later request rides on that cookie. Option and response objects cross the wire
// as JSON with base64url binary fields, converted here so this works without the
// newer PublicKeyCredential JSON helpers.
import { apiGet, apiPost, readJson } from "./apiClient";

export type AuthConfig = {
  passkeys: { available: boolean; registered: boolean };
  requirePasskey: boolean;
  rpId: string;
};

export type BrowserSession = {
  id: string;
  method: "passkey" | "token";
  passkeyName: string;
  createdAt: number;
  lastSeenAt: number;
  expiresAt: number;
  absoluteExpiresAt: number;
  userAgent: string;
  current?: boolean;
};

export type Passkey = {
  id: string;
  name: string;
  transports: string[];
  backedUp: boolean;
  createdAt: number;
  lastUsedAt: number | null;
};

export type AuthSettings = {
  requirePasskey: boolean;
  passkeyCount: number;
  sessionAbsoluteSeconds: number;
  sessionIdleSeconds: number;
  signedOutSessions?: number;
};

export class SignInUnavailable extends Error {}

type JsonRecord = Record<string, any>;

function toBuffer(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function toBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function descriptors(items: JsonRecord[] | undefined): PublicKeyCredentialDescriptor[] {
  return (items || []).map((item) => ({ ...item, id: toBuffer(String(item.id)) }) as PublicKeyCredentialDescriptor);
}

export function creationOptions(json: JsonRecord): PublicKeyCredentialCreationOptions {
  return {
    ...json,
    challenge: toBuffer(String(json.challenge)),
    user: { ...json.user, id: toBuffer(String(json.user.id)) },
    excludeCredentials: descriptors(json.excludeCredentials),
  } as PublicKeyCredentialCreationOptions;
}

export function requestOptions(json: JsonRecord): PublicKeyCredentialRequestOptions {
  return {
    ...json,
    challenge: toBuffer(String(json.challenge)),
    allowCredentials: descriptors(json.allowCredentials),
  } as PublicKeyCredentialRequestOptions;
}

function credentialBase(credential: PublicKeyCredential): JsonRecord {
  const base: JsonRecord = {
    id: credential.id,
    rawId: toBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults?.() || {},
  };
  if (credential.authenticatorAttachment) base.authenticatorAttachment = credential.authenticatorAttachment;
  return base;
}

export function registrationJson(credential: PublicKeyCredential): JsonRecord {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    ...credentialBase(credential),
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      attestationObject: toBase64url(response.attestationObject),
      transports: response.getTransports?.() || [],
    },
  };
}

export function authenticationJson(credential: PublicKeyCredential): JsonRecord {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    ...credentialBase(credential),
    response: {
      clientDataJSON: toBase64url(response.clientDataJSON),
      authenticatorData: toBase64url(response.authenticatorData),
      signature: toBase64url(response.signature),
      userHandle: response.userHandle ? toBase64url(response.userHandle) : null,
    },
  };
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential === "function" && Boolean(navigator.credentials);
}

function ceremonyError(error: unknown): Error {
  if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError")) {
    return new Error("The passkey prompt was dismissed or timed out.");
  }
  if (error instanceof DOMException && error.name === "SecurityError") {
    return new Error("Passkeys only work on the Control domain over HTTPS.");
  }
  if (error instanceof DOMException && error.name === "InvalidStateError") {
    return new Error("This authenticator already holds a passkey for this Control.");
  }
  return error instanceof Error ? error : new Error(String(error));
}

async function postJson<T>(path: string, body: unknown, headers: Record<string, string> = {}): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body ?? {}),
  });
  return readJson<T>(response);
}

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const response = await fetch("/v1/auth/config");
  if (response.status === 404) throw new SignInUnavailable("sign-in API unavailable");
  return readJson<AuthConfig>(response);
}

// The current browser session, or null when this browser is signed out.
export async function fetchCurrentSession(): Promise<BrowserSession | null> {
  const response = await fetch("/v1/auth/session");
  if (response.status === 401) return null;
  if (response.status === 404) throw new SignInUnavailable("sign-in API unavailable");
  const payload = await readJson<{ session: BrowserSession }>(response);
  return payload.session;
}

// Trade the management token for a session cookie. The token is sent once and
// is not kept anywhere in the browser afterwards.
export async function exchangeToken(token: string): Promise<BrowserSession> {
  const payload = await postJson<{ session: BrowserSession }>("/v1/auth/session", {}, { Authorization: `Bearer ${token.trim()}` });
  return payload.session;
}

export async function signOutSession(): Promise<void> {
  await postJson("/v1/auth/logout", {});
}

export async function signInWithPasskey(): Promise<BrowserSession> {
  const options = await postJson<{ challengeId: string; publicKey: JsonRecord }>("/v1/auth/passkey/authenticate/options", {});
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.get({ publicKey: requestOptions(options.publicKey) });
  } catch (error) {
    throw ceremonyError(error);
  }
  if (!credential) throw new Error("No passkey was selected.");
  const payload = await postJson<{ session: BrowserSession }>("/v1/auth/passkey/authenticate/verify", {
    challengeId: options.challengeId,
    credential: authenticationJson(credential as PublicKeyCredential),
  });
  return payload.session;
}

// Add a passkey. A signed-in operator passes their credential; a browser opened
// from a one-time enrollment link passes the code instead and is signed in.
export async function registerPasskey({ token, enrollmentCode, name }: { token?: string; enrollmentCode?: string; name: string }): Promise<{ passkey: Passkey; session?: BrowserSession }> {
  const body = enrollmentCode ? { enrollmentCode } : {};
  const options = token
    ? await apiPost<{ challengeId: string; publicKey: JsonRecord; suggestedName?: string }>("/v1/auth/passkey/register/options", token, body)
    : await postJson<{ challengeId: string; publicKey: JsonRecord; suggestedName?: string }>("/v1/auth/passkey/register/options", body);
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.create({ publicKey: creationOptions(options.publicKey) });
  } catch (error) {
    throw ceremonyError(error);
  }
  if (!credential) throw new Error("No passkey was created.");
  const verifyBody = {
    challengeId: options.challengeId,
    credential: registrationJson(credential as PublicKeyCredential),
    name: name.trim() || options.suggestedName || "",
  };
  return token
    ? apiPost("/v1/auth/passkey/register/verify", token, verifyBody)
    : postJson("/v1/auth/passkey/register/verify", verifyBody);
}

export const listPasskeys = (token: string) => apiGet<{ passkeys: Passkey[] }>("/v1/auth/passkeys", token);
export const removePasskey = (token: string, id: string) => apiPost<{ removed: string; signedOutSessions: number }>("/v1/auth/passkeys/remove", token, { id });
export const listBrowserSessions = (token: string) => apiGet<{ sessions: BrowserSession[] }>("/v1/auth/sessions", token);
export const revokeBrowserSessions = (token: string, body: { id: string } | { all: true }) => apiPost<{ revoked: number }>("/v1/auth/sessions/revoke", token, body);
export const fetchAuthSettings = (token: string) => apiGet<AuthSettings>("/v1/auth/settings", token);
export const updateAuthSettings = (token: string, requirePasskey: boolean) => apiPost<AuthSettings>("/v1/auth/settings", token, { requirePasskey });
export const createEnrollmentLink = (token: string, name: string) => apiPost<{ url: string; expiresAt: number }>("/v1/auth/enrollments", token, name.trim() ? { name: name.trim() } : {});
