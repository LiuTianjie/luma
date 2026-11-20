function randomHex(byteLen: number): string {
  const bytes = new Uint8Array(byteLen);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < byteLen; i++) {
      bytes[i] = (Math.random() * 256) | 0;
    }
  }
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

export interface TraceContext {
  traceId: string;
  spanId: string;
  traceparent: string;
}

export function createTraceContext(): TraceContext {
  const traceId = randomHex(16);
  const spanId = randomHex(8);
  return {
    traceId,
    spanId,
    traceparent: `00-${traceId}-${spanId}-01`,
  };
}

export function shouldInjectTraceHeader(targetUrl: string, traceOrigins?: Array<string | RegExp>): boolean {
  if (!targetUrl) return false;
  if (targetUrl.startsWith("/") && !targetUrl.startsWith("//")) {
    return true;
  }
  if (!traceOrigins || traceOrigins.length === 0) {
    if (typeof location !== "undefined") {
      try {
        return new URL(targetUrl, location.href).origin === location.origin;
      } catch {
        return false;
      }
    }
    return false;
  }
  for (const rule of traceOrigins) {
    if (typeof rule === "string") {
      if (targetUrl.includes(rule)) return true;
    } else if (rule.test(targetUrl)) {
      return true;
    }
  }
  return false;
}
