import type { PasskeyCredential } from "./types";

// Augment PublicKeyCredential static & instance WebAuthn L3 / Signal API methods
// (polyfilled at runtime by `webauthn-polyfills` imported in main.tsx).
interface WebAuthnClientCapabilities {
  conditionalCreate?: boolean;
  conditionalGet?: boolean;
  hybridTransport?: boolean;
  passkeyPlatformAuthenticator?: boolean;
  userVerifyingPlatformAuthenticator?: boolean;
  relatedOrigins?: boolean;
  [key: string]: boolean | undefined;
}

interface PublicKeyCredentialStaticExt {
  getClientCapabilities?: () => Promise<WebAuthnClientCapabilities>;
  parseCreationOptionsFromJSON?: (options: unknown) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (options: unknown) => PublicKeyCredentialRequestOptions;
  signalUnknownCredential?: (opts: { rpId: string; credentialId: string }) => Promise<void>;
  signalAllAcceptedCredentials?: (opts: {
    rpId: string;
    userId: string;
    allAcceptedCredentialIds: string[];
  }) => Promise<void>;
  signalCurrentUserDetails?: (opts: {
    rpId: string;
    userId: string;
    name: string;
    displayName: string;
  }) => Promise<void>;
}

interface PublicKeyCredentialInstanceExt extends PublicKeyCredential {
  toJSON(): { id: string; [key: string]: unknown };
}

function pkcStatic(): PublicKeyCredentialStaticExt | null {
  if (typeof window === "undefined" || !("PublicKeyCredential" in window) || !window.isSecureContext) {
    return null;
  }
  return window.PublicKeyCredential as unknown as PublicKeyCredentialStaticExt;
}

export function isValidBase64Url(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length % 4 !== 1 &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

export interface DetectedPasskeyCapabilities {
  supported: boolean;
  conditionalGet: boolean;
  platformAuthenticator: boolean;
}

export async function detectPasskeyCapabilities(): Promise<DetectedPasskeyCapabilities> {
  const PKC = pkcStatic();
  if (!PKC || !navigator.credentials) {
    return { supported: false, conditionalGet: false, platformAuthenticator: false };
  }
  if (typeof PKC.getClientCapabilities === "function") {
    try {
      const caps = await PKC.getClientCapabilities();
      const platform = Boolean(
        caps.passkeyPlatformAuthenticator ?? caps.userVerifyingPlatformAuthenticator,
      );
      const conditional = Boolean(caps.conditionalGet);
      return {
        supported: true,
        conditionalGet: conditional,
        platformAuthenticator: platform,
      };
    } catch {
      return { supported: false, conditionalGet: false, platformAuthenticator: false };
    }
  }
  return { supported: false, conditionalGet: false, platformAuthenticator: false };
}

async function safeSignalUnknownCredential(rpId: string | undefined, credentialId: string | undefined) {
  const PKC = pkcStatic();
  if (!PKC?.signalUnknownCredential || !rpId || !isValidBase64Url(credentialId)) return;
  try {
    await PKC.signalUnknownCredential({ rpId, credentialId });
  } catch (err) {
    console.warn("SignalUnknownCredential sync warning:", err);
  }
}

export async function syncAllAcceptedCredentials(
  rpId: string | undefined,
  userId: string | undefined,
  credentials: PasskeyCredential[],
): Promise<void> {
  const PKC = pkcStatic();
  if (!PKC?.signalAllAcceptedCredentials) return;
  const credentialIds = credentials.map((c) => c.id);
  if (!rpId || !isValidBase64Url(userId) || !credentialIds.every(isValidBase64Url)) {
    console.warn(
      "SignalAllAcceptedCredentials skipped: server rpId and valid unpadded Base64URL userId and credential IDs are required.",
    );
    return;
  }
  try {
    await PKC.signalAllAcceptedCredentials({
      rpId,
      userId,
      allAcceptedCredentialIds: credentialIds,
    });
  } catch (err) {
    console.warn("SignalAllAcceptedCredentials sync warning:", err);
  }
}

export async function syncCurrentUserDetails(
  rpId: string | undefined,
  userId: string | undefined,
  name: string,
  displayName: string,
): Promise<void> {
  const PKC = pkcStatic();
  if (!PKC?.signalCurrentUserDetails) return;
  if (!rpId || !isValidBase64Url(userId)) {
    console.warn(
      "SignalCurrentUserDetails skipped: server rpId and valid unpadded Base64URL userId are required.",
    );
    return;
  }
  try {
    await PKC.signalCurrentUserDetails({
      rpId,
      userId,
      name,
      displayName,
    });
  } catch (err) {
    console.warn("SignalCurrentUserDetails sync warning:", err);
  }
}

export async function runConditionalPasskeyAutofill(
  signal: AbortSignal,
  onAuthenticated: () => Promise<void>,
  onError?: (msg: string) => void,
): Promise<void> {
  const PKC = pkcStatic();
  if (!PKC?.getClientCapabilities || !PKC.parseRequestOptionsFromJSON) return;

  const caps = await PKC.getClientCapabilities().catch(() => null);
  if (!caps?.conditionalGet || signal.aborted) return;

  const beginResp = await fetch("/api/v1/auth/passkeys/login/begin", {
    method: "POST",
    credentials: "same-origin",
    signal,
  });
  if (!beginResp.ok || signal.aborted) return;
  const loginOptionsJSON = await beginResp.json();
  const publicKey = PKC.parseRequestOptionsFromJSON(loginOptionsJSON);

  let cred: PublicKeyCredentialInstanceExt | null = null;
  try {
    cred = (await navigator.credentials.get({
      publicKey,
      signal,
      mediation: "conditional" as CredentialMediationRequirement,
    })) as PublicKeyCredentialInstanceExt | null;
  } catch (err) {
    if (err instanceof DOMException && (err.name === "NotAllowedError" || err.name === "AbortError")) {
      return;
    }
    return;
  }
  if (!cred || !cred.toJSON) return;

  const encoded = cred.toJSON();
  try {
    const finResp = await fetch("/api/v1/auth/passkeys/login/finish", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(encoded),
    });
    if (!finResp.ok) {
      if (finResp.status === 404) {
        // Always read rpId from server-returned publicKey.rpId, NEVER window.location.hostname
        await safeSignalUnknownCredential(publicKey.rpId, encoded.id);
      }
      const body = await finResp.json().catch(() => null);
      onError?.(body?.error || `Passkey 验证失败 (HTTP ${finResp.status})`);
      return;
    }
    await onAuthenticated();
  } catch (err) {
    onError?.(err instanceof Error ? err.message : "Passkey 登录网络异常");
  }
}

export async function triggerExplicitPasskeyLogin(
  signal: AbortSignal,
): Promise<{ authenticated: boolean; cancelled?: boolean }> {
  const PKC = pkcStatic();
  if (!PKC?.parseRequestOptionsFromJSON) {
    throw new Error("当前浏览器环境不支持 WebAuthn Passkey");
  }

  const beginResp = await fetch("/api/v1/auth/passkeys/login/begin", {
    method: "POST",
    credentials: "same-origin",
    signal,
  });
  if (!beginResp.ok) {
    const body = await beginResp.json().catch(() => null);
    throw new Error(body?.error || `无法发起 Passkey 登录 (HTTP ${beginResp.status})`);
  }
  const loginOptionsJSON = await beginResp.json();
  const publicKey = PKC.parseRequestOptionsFromJSON(loginOptionsJSON);

  let cred: PublicKeyCredentialInstanceExt | null = null;
  try {
    cred = (await navigator.credentials.get({
      publicKey,
      signal,
    })) as PublicKeyCredentialInstanceExt | null;
  } catch (err) {
    if (err instanceof DOMException && (err.name === "NotAllowedError" || err.name === "AbortError")) {
      return { authenticated: false, cancelled: true };
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
  if (!cred || !cred.toJSON) {
    return { authenticated: false, cancelled: true };
  }

  const encoded = cred.toJSON();
  const finResp = await fetch("/api/v1/auth/passkeys/login/finish", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(encoded),
  });
  if (!finResp.ok) {
    if (finResp.status === 404) {
      await safeSignalUnknownCredential(publicKey.rpId, encoded.id);
    }
    const body = await finResp.json().catch(() => null);
    throw new Error(body?.error || `Passkey 验证未通过 (HTTP ${finResp.status})`);
  }
  return { authenticated: true };
}

export async function registerPasskeyCredential(opts: {
  name?: string;
  promotion?: boolean;
}): Promise<{
  cancelled?: boolean;
  passkey?: PasskeyCredential;
  rpId?: string;
  userId?: string;
}> {
  const PKC = pkcStatic();
  if (!PKC?.parseCreationOptionsFromJSON) {
    throw new Error("当前浏览器环境不支持 Passkey 注册");
  }

  const beginResp = await fetch("/api/v1/auth/passkeys/register/begin", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: opts.name?.trim() || "",
      promotion: Boolean(opts.promotion),
    }),
  });
  if (!beginResp.ok) {
    const body = await beginResp.json().catch(() => null);
    throw new Error(body?.error || `无法发起 Passkey 注册 (HTTP ${beginResp.status})`);
  }

  const creationOptionsJSON = await beginResp.json();
  const publicKey = PKC.parseCreationOptionsFromJSON(creationOptionsJSON);

  let cred: PublicKeyCredentialInstanceExt | null = null;
  try {
    cred = (await navigator.credentials.create({
      publicKey,
    })) as PublicKeyCredentialInstanceExt | null;
  } catch (err) {
    if (err instanceof DOMException) {
      if (err.name === "NotAllowedError" || err.name === "AbortError") {
        return { cancelled: true };
      }
      if (err.name === "InvalidStateError") {
        throw new Error("当前密码管理器或设备已注册过此账号的 Passkey");
      }
      if (err.name === "SecurityError") {
        throw new Error("安全上下文或 RP ID 域名不匹配（请通过正式 HTTPS 域名或 localhost 访问）");
      }
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
  if (!cred || !cred.toJSON) {
    return { cancelled: true };
  }

  const encoded = cred.toJSON();
  try {
    const finResp = await fetch("/api/v1/auth/passkeys/register/finish", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(encoded),
    });
    if (!finResp.ok) {
      await safeSignalUnknownCredential(publicKey.rp.id, encoded.id);
      const body = await finResp.json().catch(() => null);
      throw new Error(body?.error || `Passkey 注册校验失败 (HTTP ${finResp.status})`);
    }
    const data = (await finResp.json()) as {
      passkey: PasskeyCredential;
      rpId: string;
      userId: string;
    };
    return {
      passkey: data.passkey,
      rpId: data.rpId,
      userId: data.userId,
    };
  } catch (err) {
    if (!(err instanceof Error && err.message.includes("Passkey 注册校验失败"))) {
      await safeSignalUnknownCredential(publicKey.rp.id, encoded.id);
    }
    throw err;
  }
}
