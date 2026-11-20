import { scrubUrl } from "./breadcrumbs";

export interface SerializedNode {
  /** Monotonic node ID within the session snapshot stream */
  id: number;
  /** 0 = document, 1 = doctype, 2 = element, 3 = text */
  t: 0 | 1 | 2 | 3;
  /** Doctype name or Element lowercase tagName */
  tag?: string;
  /** Sanitized HTML/SVG attributes (plus _val / _chk for form state) */
  a?: Record<string, string>;
  /** Child nodes */
  ch?: SerializedNode[];
  /** Text content for t === 3 */
  tx?: string;
  /** Inlined stylesheet rules for <link rel="stylesheet"> or <style> */
  css?: string;
  /** Non-zero [width, height] when [data-eye-block] replaces subtree */
  blocked?: [number, number];
  /** Non-zero [width, height] for <canvas> / <video> / <iframe> placeholders */
  box?: [number, number];
  /** 1 if node is within SVG namespace */
  svg?: 1;
}

export type ReplayEvent =
  | {
      /** 1 = Meta / Viewport / Route update */
      t: 1;
      ts: number;
      href: string;
      w: number;
      h: number;
      title?: string;
    }
  | {
      /** 2 = Full DOM Snapshot */
      t: 2;
      ts: number;
      href: string;
      w: number;
      h: number;
      root: SerializedNode;
    }
  | {
      /** 3 = Incremental DOM Mutation Batch */
      t: 3;
      ts: number;
      rm?: number[];
      ad?: Array<{ p: number; nx: number | null; n: SerializedNode }>;
      tx?: Array<{ id: number; v: string }>;
      at?: Array<{ id: number; k: string; v: string | null }>;
    }
  | {
      /** 4 = Sampled Mouse Pointer Trail ([x, y, dtMsFromEventTs]) */
      t: 4;
      ts: number;
      pts: Array<[number, number, number]>;
    }
  | {
      /** 5 = Pointer Click / Tap */
      t: 5;
      ts: number;
      x: number;
      y: number;
      id?: number;
      tag?: string;
      label?: string;
    }
  | {
      /** 6 = Window (id=0) or Element Scroll */
      t: 6;
      ts: number;
      id: number;
      x: number;
      y: number;
    }
  | {
      /** 7 = Masked Form Input / Checked Sync */
      t: 7;
      ts: number;
      id: number;
      v?: string;
      c?: boolean;
    }
  | {
      /** 8 = Dynamic CSSRule Insertion */
      t: 8;
      ts: number;
      id?: number;
      css: string;
    };

export interface SnapshotOptions {
  maskAllInputs?: boolean;
  maxNodes?: number;
  maxDepth?: number;
  maxInlineCssBytes?: number;
}

export interface ReplayCollectorOptions {
  endpointUrl: string;
  publicKey: string;
  project: string;
  env?: string;
  release?: string;
  maskAllInputs?: boolean;
  flushIntervalMs?: number;
  getSessionId: () => string;
  getActorId: () => string;
}

export interface ReplayController {
  flushNow(urgent?: boolean): void;
  stop(): void;
}

const SENSITIVE_ATTR_RE =
  /pass|pwd|token|secret|(?:api|access|private|secret)[\s_-]?key|mnemonic|seed|card|cc-|cvv|cvc|iban|ssn|phone|mobile|tel|id[\s_-]?card|bank|auth[\s_-]?code|otp/i;
const CARD_DIGITS_RE = /\b(?:\d[ -]*?){13,19}\b/;
const JWT_LIKE_RE = /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/;
const CSS_URL_RE = /url\(\s*(['"]?)(?!data:|blob:|https?:|\/\/|#)([^'")\s]+)\1\s*\)/gi;

const SKIP_TAGS = new Set(["script", "noscript", "applet", "object", "embed"]);
const URL_ATTRS = new Set(["src", "href", "poster", "action"]);

export function maskValue(raw: string): string {
  if (!raw) return "";
  const len = Math.min(Math.max(raw.trim().length, 6), 14);
  return "•".repeat(len);
}

export function shouldMaskInput(el: Element, maskAll = false): boolean {
  if (maskAll) return true;
  if (typeof el.closest === "function" && el.closest("[data-eye-mask],[data-eye-block]")) {
    return true;
  }
  const tag = el.tagName.toLowerCase();
  if (tag !== "input" && tag !== "textarea") return false;
  const type = (el.getAttribute("type") || "text").toLowerCase();
  if (type === "password" || type === "email" || type === "tel" || type === "hidden") {
    return true;
  }
  const hint = [
    el.getAttribute("name"),
    el.getAttribute("id"),
    el.getAttribute("autocomplete"),
    el.getAttribute("placeholder"),
    el.getAttribute("aria-label"),
    el.getAttribute("data-testid"),
  ]
    .filter(Boolean)
    .join(" ");
  if (hint && SENSITIVE_ATTR_RE.test(hint)) {
    return true;
  }
  const val = (el as HTMLInputElement | HTMLTextAreaElement).value || "";
  if (val && (CARD_DIGITS_RE.test(val) || JWT_LIKE_RE.test(val))) {
    return true;
  }
  return false;
}

function resolveAttrUrl(raw: string, baseUri: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  if (/^javascript:/i.test(trimmed)) return "about:blank";
  if (
    trimmed.startsWith("data:") ||
    trimmed.startsWith("blob:") ||
    trimmed.startsWith("#") ||
    trimmed.startsWith("mailto:") ||
    trimmed.startsWith("tel:")
  ) {
    return trimmed;
  }
  try {
    return scrubUrl(new URL(trimmed, baseUri).href);
  } catch {
    return trimmed;
  }
}

function rewriteCssUrls(cssText: string, baseHref: string): string {
  if (!baseHref || !cssText.includes("url(")) return cssText;
  return cssText.replace(CSS_URL_RE, (_m, _q, rel) => {
    try {
      const abs = new URL(rel, baseHref).href;
      return `url("${abs}")`;
    } catch {
      return _m;
    }
  });
}

function extractInlineCss(el: Element, docBase: string, remainingBudget: number): string | undefined {
  if (remainingBudget <= 0) return undefined;
  const tag = el.tagName.toLowerCase();
  let sheet: CSSStyleSheet | null = null;
  if (tag === "link") {
    const rel = (el.getAttribute("rel") || "").toLowerCase();
    if (!rel.includes("stylesheet")) return undefined;
    sheet = (el as HTMLLinkElement).sheet;
  } else if (tag === "style") {
    sheet = (el as HTMLStyleElement).sheet;
  }
  if (!sheet) return undefined;
  try {
    const rules = sheet.cssRules;
    if (!rules || rules.length === 0) return undefined;
    const chunks: string[] = [];
    let total = 0;
    const baseHref = sheet.href || docBase;
    for (let i = 0; i < rules.length; i++) {
      const txt = rewriteCssUrls(rules[i].cssText, baseHref);
      total += txt.length + 1;
      if (total > remainingBudget) return undefined;
      chunks.push(txt);
    }
    return chunks.join("\n");
  } catch {
    // Cross-origin stylesheet without CORS: browser will still load <link href="..."> directly in replay iframe
    return undefined;
  }
}

function measureBox(el: Element): [number, number] | undefined {
  if (typeof (el as HTMLElement).getBoundingClientRect !== "function") return undefined;
  try {
    const r = (el as HTMLElement).getBoundingClientRect();
    const w = Math.round(r.width);
    const h = Math.round(r.height);
    if (w > 0 && h > 0) return [w, h];
  } catch {
    // ignore
  }
  return undefined;
}

interface SerializerContext {
  nodeToId: WeakMap<Node, number>;
  nextId: number;
  nodeCount: number;
  maxNodes: number;
  maxDepth: number;
  cssBudget: number;
  baseUri: string;
  maskAllInputs: boolean;
}

function assignId(ctx: SerializerContext, node: Node): number {
  const existing = ctx.nodeToId.get(node);
  if (existing !== undefined) return existing;
  const id = ctx.nextId++;
  ctx.nodeToId.set(node, id);
  return id;
}

function serializeNode(
  node: Node,
  ctx: SerializerContext,
  depth: number,
  inMask: boolean,
  inSvg: boolean
): SerializedNode | null {
  if (ctx.nodeCount >= ctx.maxNodes || depth > ctx.maxDepth) return null;

  const nodeType = node.nodeType;
  // 9 = DOCUMENT_NODE
  if (nodeType === 9) {
    ctx.nodeCount++;
    const id = assignId(ctx, node);
    const ch: SerializedNode[] = [];
    const childNodes = node.childNodes;
    for (let i = 0; i < childNodes.length; i++) {
      const c = serializeNode(childNodes[i], ctx, depth + 1, inMask, false);
      if (c) ch.push(c);
    }
    return { id, t: 0, ch };
  }

  // 10 = DOCUMENT_TYPE_NODE
  if (nodeType === 10) {
    ctx.nodeCount++;
    const id = assignId(ctx, node);
    return { id, t: 1, tag: (node as DocumentType).name || "html" };
  }

  // 3 = TEXT_NODE
  if (nodeType === 3) {
    const parentTag = node.parentElement?.tagName.toLowerCase();
    if (parentTag && SKIP_TAGS.has(parentTag)) return null;
    const raw = node.nodeValue ?? "";
    if (!raw) return null;
    ctx.nodeCount++;
    const id = assignId(ctx, node);
    const tx = inMask ? raw.replace(/\S/g, "•") : raw.slice(0, 8000);
    return { id, t: 3, tx };
  }

  // 1 = ELEMENT_NODE
  if (nodeType === 1) {
    const el = node as Element;
    const tag = el.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return null;
    if (tag === "meta") {
      const httpEquiv = (el.getAttribute("http-equiv") || "").toLowerCase();
      if (httpEquiv === "content-security-policy" || httpEquiv === "refresh") {
        return null;
      }
    }
    if (tag === "link") {
      const rel = (el.getAttribute("rel") || "").toLowerCase();
      if (rel === "preload" || rel === "modulepreload" || rel === "prefetch" || rel === "dns-prefetch") {
        return null;
      }
    }

    ctx.nodeCount++;
    const id = assignId(ctx, el);
    const isSvg = inSvg || tag === "svg";

    if (el.hasAttribute("data-eye-block")) {
      const box = measureBox(el) || [160, 40];
      return { id, t: 2, tag: "div", blocked: box };
    }

    const nextMask = inMask || el.hasAttribute("data-eye-mask");
    const attrs: Record<string, string> = {};
    const attrNames = el.getAttributeNames ? el.getAttributeNames() : [];
    for (let i = 0; i < attrNames.length; i++) {
      const k = attrNames[i];
      const lk = k.toLowerCase();
      if (lk.startsWith("on") || lk === "nonce" || lk === "integrity") continue;
      const v = el.getAttribute(k);
      if (v === null) continue;
      if (URL_ATTRS.has(lk)) {
        attrs[k] = resolveAttrUrl(v, ctx.baseUri);
      } else {
        attrs[k] = v.slice(0, 2048);
      }
    }

    // Live form input values
    if (tag === "input" || tag === "textarea" || tag === "select") {
      const inputEl = el as HTMLInputElement;
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (type === "checkbox" || type === "radio") {
        if (inputEl.checked) attrs._chk = "1";
      } else if (typeof inputEl.value === "string" && inputEl.value !== "") {
        const masked = nextMask || shouldMaskInput(el, ctx.maskAllInputs) ? maskValue(inputEl.value) : inputEl.value.slice(0, 512);
        attrs._val = masked;
      }
    }

    const out: SerializedNode = { id, t: 2, tag };
    if (isSvg) out.svg = 1;
    if (Object.keys(attrs).length > 0) out.a = attrs;

    if (tag === "canvas" || tag === "video" || tag === "iframe") {
      const box = measureBox(el);
      if (box) out.box = box;
      // Do not descend into cross-origin/nested iframes
      if (tag === "iframe") return out;
    }

    if (tag === "link" || tag === "style") {
      const inlined = extractInlineCss(el, ctx.baseUri, ctx.cssBudget);
      if (inlined) {
        out.css = inlined;
        ctx.cssBudget -= inlined.length;
      }
    }

    const ch: SerializedNode[] = [];
    const childNodes = el.childNodes;
    for (let i = 0; i < childNodes.length; i++) {
      const c = serializeNode(childNodes[i], ctx, depth + 1, nextMask, isSvg);
      if (c) ch.push(c);
    }
    if (ch.length > 0) out.ch = ch;
    return out;
  }

  return null;
}

export function serializeDomTree(doc: Document, opts: SnapshotOptions = {}) {
  const baseUri = scrubUrl(doc.baseURI || (typeof location !== "undefined" ? location.href : "https://localhost/"));
  const ctx: SerializerContext = {
    nodeToId: new WeakMap<Node, number>(),
    nextId: 1,
    nodeCount: 0,
    maxNodes: opts.maxNodes ?? 8500,
    maxDepth: opts.maxDepth ?? 45,
    cssBudget: opts.maxInlineCssBytes ?? 245_760,
    baseUri,
    maskAllInputs: Boolean(opts.maskAllInputs),
  };
  const root = serializeNode(doc, ctx, 0, false, false) || { id: 1, t: 0, ch: [] };
  return { root, ctx };
}

function purgeSubtreeIds(node: Node, map: WeakMap<Node, number>, removedOut?: number[]): void {
  const id = map.get(node);
  if (id !== undefined) {
    if (removedOut) removedOut.push(id);
    map.delete(node);
  }
  const children = node.childNodes;
  if (!children) return;
  for (let i = 0; i < children.length; i++) {
    purgeSubtreeIds(children[i], map);
  }
}

function isInsideBlocked(node: Node | null): boolean {
  let cur: Node | null = node;
  while (cur) {
    if (cur.nodeType === 1 && (cur as Element).hasAttribute("data-eye-block")) return true;
    cur = cur.parentNode;
  }
  return false;
}

function isInsideMasked(node: Node | null): boolean {
  let cur: Node | null = node;
  while (cur) {
    if (cur.nodeType === 1 && (cur as Element).hasAttribute("data-eye-mask")) return true;
    cur = cur.parentNode;
  }
  return false;
}

function isInsideSvg(node: Node | null): boolean {
  let cur: Node | null = node;
  while (cur) {
    if (cur.nodeType === 1 && (cur as Element).tagName.toLowerCase() === "svg") return true;
    cur = cur.parentNode;
  }
  return false;
}

function describeClickLabel(el: Element | null): { tag: string; label?: string } {
  if (!el || typeof el.tagName !== "string") return { tag: "element" };
  const interactive = typeof el.closest === "function" ? el.closest("button,a,[role='button'],input,select,summary,label") || el : el;
  const tag = interactive.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea") {
    const type = (interactive.getAttribute("type") || "text").toLowerCase();
    return { tag: `input[${type}]` };
  }
  const text = ((interactive as HTMLElement).innerText || interactive.getAttribute("aria-label") || "")
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, 36);
  return { tag, label: text || undefined };
}

async function compressGzipIfSupported(jsonStr: string): Promise<Blob | string> {
  if (jsonStr.length < 1024 || typeof CompressionStream !== "function" || typeof Blob !== "function") {
    return jsonStr;
  }
  try {
    const enc = new TextEncoder().encode(jsonStr);
    const cs = new CompressionStream("gzip");
    const writer = cs.writable.getWriter();
    void writer.write(enc);
    void writer.close();
    const reader = cs.readable.getReader();
    const parts: Uint8Array[] = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) parts.push(value);
    }
    // Use CORS-safelisted text/plain so cross-origin POST skips OPTIONS preflight;
    // backend detects RFC 1952 gzip magic header (0x1f 0x8b) deterministically.
    return new Blob(parts as BlobPart[], { type: "text/plain" });
  } catch {
    return jsonStr;
  }
}

function scheduleBackground(fn: () => void): void {
  const g = globalThis as unknown as {
    scheduler?: { postTask?: (cb: () => void, opts?: { priority?: string }) => Promise<void> };
    requestIdleCallback?: (cb: () => void, opts?: { timeout?: number }) => number;
  };
  if (g.scheduler && typeof g.scheduler.postTask === "function") {
    void g.scheduler.postTask(fn, { priority: "background" }).catch(() => {});
    return;
  }
  if (typeof g.requestIdleCallback === "function") {
    g.requestIdleCallback(fn, { timeout: 1500 });
    return;
  }
  setTimeout(fn, 0);
}

export function startSessionReplay(opts: ReplayCollectorOptions): ReplayController {
  if (
    typeof window === "undefined" ||
    typeof document === "undefined" ||
    typeof MutationObserver === "undefined" ||
    !document.documentElement
  ) {
    return { flushNow() {}, stop() {} };
  }

  const win = window as unknown as Record<string, unknown>;
  if (win.__lumaEyeReplayCtrl) {
    try {
      (win.__lumaEyeReplayCtrl as ReplayController).stop();
    } catch {
      // ignore
    }
  }

  let stopped = false;
  let seq = 0;
  let activeSessionId = opts.getSessionId();
  let lastHref = scrubUrl(location.href);
  let lastActivityAt = Date.now();
  const MAX_IDLE_MS = 15 * 60 * 1000;

  let eventsBuffer: ReplayEvent[] = [];
  let mousePts: Array<[number, number, number]> = [];
  let mouseBatchStartTs = 0;
  let lastMouseSampleTs = 0;

  let serializerCtx: SerializerContext | null = null;

  const replayEndpoint = opts.endpointUrl
    .replace(/\/_e\/b$/, "/_e/r")
    .replace(/\/v1\/b$/, "/_e/r")
    .replace(/\/+$/, "");

  const emit = (ev: ReplayEvent) => {
    if (stopped) return;
    // Detect session rotation after idle timeout: flush old session and capture fresh root snapshot
    const latestSid = opts.getSessionId();
    if (latestSid && latestSid !== activeSessionId) {
      flushSegment(false);
      activeSessionId = latestSid;
      seq = 0;
      takeFullSnapshot();
    }
    eventsBuffer.push(ev);
    if (eventsBuffer.length >= 140) {
      flushSegment(false);
    }
  };

  const flushMouseTrail = () => {
    if (mousePts.length === 0) return;
    const pts = mousePts;
    const ts = mouseBatchStartTs || Date.now();
    mousePts = [];
    mouseBatchStartTs = 0;
    emit({ t: 4, ts, pts });
  };

  const takeFullSnapshot = () => {
    if (stopped || !document.documentElement) return;
    const now = Date.now();
    lastHref = scrubUrl(location.href);
    const vw = Math.round(window.innerWidth || document.documentElement.clientWidth || 1280);
    const vh = Math.round(window.innerHeight || document.documentElement.clientHeight || 720);
    const snap = serializeDomTree(document, {
      maskAllInputs: opts.maskAllInputs,
    });
    serializerCtx = snap.ctx;
    eventsBuffer.push({
      t: 1,
      ts: now,
      href: lastHref,
      w: vw,
      h: vh,
      title: (document.title || "").slice(0, 120),
    });
    eventsBuffer.push({
      t: 2,
      ts: now,
      href: lastHref,
      w: vw,
      h: vh,
      root: snap.root,
    });
    // Record initial scroll if scrolled down
    const sy = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
    const sx = Math.round(window.scrollX || document.documentElement.scrollLeft || 0);
    if (sx > 0 || sy > 0) {
      eventsBuffer.push({ t: 6, ts: now, id: 0, x: sx, y: sy });
    }
  };

  const sendPayload = async (jsonStr: string, url: string, urgent: boolean) => {
    if (urgent && jsonStr.length < 58_000 && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      try {
        if (navigator.sendBeacon(url, jsonStr)) return;
      } catch {
        // fall through to fetch
      }
    }
    const body = urgent ? jsonStr : await compressGzipIfSupported(jsonStr);
    const sizeEstimate = typeof body === "string" ? body.length : body.size;
    try {
      await fetch(url, {
        method: "POST",
        body,
        credentials: "omit",
        keepalive: urgent && sizeEstimate < 60_000,
        priority: "low",
      } as RequestInit);
    } catch {
      // Drop silently on offline/network error to protect host app
    }
  };

  const flushSegment = (urgent: boolean) => {
    flushMouseTrail();
    flushPendingMutations();
    if (eventsBuffer.length === 0) return;

    const batch = eventsBuffer;
    eventsBuffer = [];
    const currentSeq = seq++;
    const sid = activeSessionId || opts.getSessionId();
    const aid = opts.getActorId();
    if (!sid) return;

    const startMs = batch[0].ts;
    const endMs = batch[batch.length - 1].ts;
    const hasFull = batch.some((e) => e.t === 2) ? 1 : 0;
    const vw = Math.round(window.innerWidth || 1280);
    const vh = Math.round(window.innerHeight || 720);

    const payload = JSON.stringify({
      project: opts.project,
      env: opts.env || "production",
      release: opts.release || "",
      session_id: sid,
      actor_id: aid,
      seq: currentSeq,
      start_ms: startMs,
      end_ms: endMs,
      has_full_snapshot: hasFull,
      page_url: lastHref,
      viewport_w: vw,
      viewport_h: vh,
      events: batch,
    });

    const qs = new URLSearchParams({
      k: opts.publicKey,
      p: opts.project,
      s: sid,
      seq: String(currentSeq),
    });
    const targetUrl = `${replayEndpoint}?${qs.toString()}`;

    if (urgent) {
      void sendPayload(payload, targetUrl, true);
    } else {
      scheduleBackground(() => {
        void sendPayload(payload, targetUrl, false);
      });
    }
  };

  // --- Mutation batching (coalesced per animation frame / 100ms) ---
  let pendingRecords: MutationRecord[] = [];
  let mutationTimer: ReturnType<typeof setTimeout> | null = null;

  const flushPendingMutations = () => {
    if (mutationTimer !== null) {
      clearTimeout(mutationTimer);
      mutationTimer = null;
    }
    if (pendingRecords.length === 0 || !serializerCtx) return;
    const records = pendingRecords;
    pendingRecords = [];

    // Check if SPA route changed alongside DOM mutation
    const curHref = scrubUrl(location.href);
    if (curHref !== lastHref) {
      lastHref = curHref;
      emit({
        t: 1,
        ts: Date.now(),
        href: curHref,
        w: Math.round(window.innerWidth || 1280),
        h: Math.round(window.innerHeight || 720),
        title: (document.title || "").slice(0, 120),
      });
    }

    const ctx = serializerCtx;
    ctx.baseUri = lastHref;
    // Refresh per-batch node budget for incremental diffs
    ctx.nodeCount = 0;
    ctx.maxNodes = 2500;

    const removedIds: number[] = [];
    const touchedParents = new Set<Node>();
    const dirtyNewNodes = new Set<Node>();
    const textUpdates = new Map<Node, string>();
    const attrUpdates = new Map<Element, Set<string>>();

    for (let i = 0; i < records.length; i++) {
      const rec = records[i];
      if (isInsideBlocked(rec.target)) continue;

      if (rec.type === "childList") {
        for (let j = 0; j < rec.removedNodes.length; j++) {
          const rn = rec.removedNodes[j];
          purgeSubtreeIds(rn, ctx.nodeToId, removedIds);
        }
        if (rec.addedNodes.length > 0 && rec.target.isConnected && ctx.nodeToId.has(rec.target)) {
          let hasRelevantAdd = false;
          for (let j = 0; j < rec.addedNodes.length; j++) {
            const an = rec.addedNodes[j];
            if (!an.isConnected) continue;
            if (an.nodeType === 1 && SKIP_TAGS.has((an as Element).tagName.toLowerCase())) continue;
            // If node was moved inside parent, purge old id first so right-to-left placement is clean
            purgeSubtreeIds(an, ctx.nodeToId, removedIds);
            dirtyNewNodes.add(an);
            hasRelevantAdd = true;
          }
          if (hasRelevantAdd) touchedParents.add(rec.target);
        }
      } else if (rec.type === "characterData") {
        if (rec.target.isConnected && ctx.nodeToId.has(rec.target)) {
          const raw = rec.target.nodeValue ?? "";
          textUpdates.set(rec.target, isInsideMasked(rec.target) ? raw.replace(/\S/g, "•") : raw.slice(0, 8000));
        }
      } else if (rec.type === "attributes" && rec.attributeName) {
        const el = rec.target as Element;
        const lk = rec.attributeName.toLowerCase();
        if (!lk.startsWith("on") && lk !== "nonce" && lk !== "integrity" && el.isConnected && ctx.nodeToId.has(el)) {
          let set = attrUpdates.get(el);
          if (!set) {
            set = new Set<string>();
            attrUpdates.set(el, set);
          }
          set.add(rec.attributeName);
        }
      }
    }

    // Walk each touched parent's childNodes right-to-left so `nx` (nextSibling ID) is always already present!
    const adds: Array<{ p: number; nx: number | null; n: SerializedNode }> = [];
    // Process shallower parents first so nested touched parents already have IDs if needed
    const sortedParents = Array.from(touchedParents);
    for (let i = 0; i < sortedParents.length; i++) {
      const parent = sortedParents[i];
      const parentId = ctx.nodeToId.get(parent);
      if (parentId === undefined || !parent.isConnected) continue;

      const inMask = isInsideMasked(parent);
      const inSvg = isInsideSvg(parent);
      const children = parent.childNodes;
      let nextSiblingId: number | null = null;
      const parentAddsReversed: Array<{ p: number; nx: number | null; n: SerializedNode }> = [];

      for (let idx = children.length - 1; idx >= 0; idx--) {
        const child = children[idx];
        const existingId = ctx.nodeToId.get(child);
        if (existingId !== undefined && !dirtyNewNodes.has(child)) {
          nextSiblingId = existingId;
          continue;
        }
        const serialized = serializeNode(child, ctx, 1, inMask, inSvg);
        if (serialized) {
          parentAddsReversed.push({ p: parentId, nx: nextSiblingId, n: serialized });
          nextSiblingId = serialized.id;
        }
      }
      // Emit right-to-left insertions in that exact execution order so `nx` always exists in parent!
      for (let k = 0; k < parentAddsReversed.length; k++) {
        adds.push(parentAddsReversed[k]);
      }
    }

    const txOut: Array<{ id: number; v: string }> = [];
    textUpdates.forEach((v, node) => {
      const id = ctx.nodeToId.get(node);
      if (id !== undefined) txOut.push({ id, v });
    });

    const atOut: Array<{ id: number; k: string; v: string | null }> = [];
    attrUpdates.forEach((keys, el) => {
      const id = ctx.nodeToId.get(el);
      if (id === undefined) return;
      keys.forEach((k) => {
        const raw = el.getAttribute(k);
        if (raw === null) {
          atOut.push({ id, k, v: null });
        } else if (URL_ATTRS.has(k.toLowerCase())) {
          atOut.push({ id, k, v: resolveAttrUrl(raw, ctx.baseUri) });
        } else {
          atOut.push({ id, k, v: raw.slice(0, 2048) });
        }
      });
    });

    if (removedIds.length === 0 && adds.length === 0 && txOut.length === 0 && atOut.length === 0) {
      return;
    }

    const ev: Extract<ReplayEvent, { t: 3 }> = { t: 3, ts: Date.now() };
    if (removedIds.length > 0) ev.rm = Array.from(new Set(removedIds));
    if (adds.length > 0) ev.ad = adds;
    if (txOut.length > 0) ev.tx = txOut;
    if (atOut.length > 0) ev.at = atOut;
    emit(ev);
  };

  // --- Start initial snapshot & observers ---
  takeFullSnapshot();

  const observer = new MutationObserver((mutations) => {
    if (stopped || Date.now() - lastActivityAt > MAX_IDLE_MS) return;
    for (let i = 0; i < mutations.length; i++) {
      pendingRecords.push(mutations[i]);
    }
    if (mutationTimer === null) {
      mutationTimer = setTimeout(flushPendingMutations, 90);
    }
  });

  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });

  const cleanups: Array<() => void> = [() => observer.disconnect()];

  // Pointer trail (sampled at 80ms)
  const onPointerMove = (ev: MouseEvent | PointerEvent) => {
    const now = Date.now();
    lastActivityAt = now;
    if (now - lastMouseSampleTs < 80) return;
    lastMouseSampleTs = now;
    if (mousePts.length === 0) {
      mouseBatchStartTs = now;
    }
    mousePts.push([Math.round(ev.clientX), Math.round(ev.clientY), now - mouseBatchStartTs]);
    if (mousePts.length >= 25) {
      flushMouseTrail();
    }
  };
  document.addEventListener("pointermove", onPointerMove, { passive: true });
  cleanups.push(() => document.removeEventListener("pointermove", onPointerMove));

  // Click / Tap
  let lastClickTs = 0;
  const onClick = (ev: MouseEvent) => {
    const now = Date.now();
    lastActivityAt = now;
    if (now - lastClickTs < 50) return;
    lastClickTs = now;
    flushMouseTrail();
    const target = ev.target instanceof Element ? ev.target : null;
    const nodeId = target && serializerCtx ? serializerCtx.nodeToId.get(target) : undefined;
    const { tag, label } = describeClickLabel(target);
    emit({
      t: 5,
      ts: now,
      x: Math.round(ev.clientX),
      y: Math.round(ev.clientY),
      id: nodeId,
      tag,
      label,
    });
  };
  document.addEventListener("click", onClick, { capture: true, passive: true });
  cleanups.push(() => document.removeEventListener("click", onClick, { capture: true }));

  // Scroll (window & scrollable inner containers, throttled 120ms)
  let lastScrollTs = 0;
  const onScroll = (ev: Event) => {
    const now = Date.now();
    lastActivityAt = now;
    if (now - lastScrollTs < 120) return;
    lastScrollTs = now;
    const target = ev.target;
    if (!target || target === document || target === document.documentElement || target === document.body) {
      emit({
        t: 6,
        ts: now,
        id: 0,
        x: Math.round(window.scrollX || document.documentElement.scrollLeft || 0),
        y: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
      });
    } else if (target instanceof HTMLElement && serializerCtx) {
      const id = serializerCtx.nodeToId.get(target);
      if (id !== undefined) {
        emit({
          t: 6,
          ts: now,
          id,
          x: Math.round(target.scrollLeft),
          y: Math.round(target.scrollTop),
        });
      }
    }
  };
  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  cleanups.push(() => document.removeEventListener("scroll", onScroll, { capture: true }));

  // Input / Change sync (always masked via shouldMaskInput)
  const onInput = (ev: Event) => {
    const now = Date.now();
    lastActivityAt = now;
    const target = ev.target;
    if (!(target instanceof HTMLElement) || !serializerCtx) return;
    if (isInsideBlocked(target)) return;
    const id = serializerCtx.nodeToId.get(target);
    if (id === undefined) return;
    const tag = target.tagName.toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") return;
    const inputEl = target as HTMLInputElement;
    const type = (target.getAttribute("type") || "").toLowerCase();
    if (type === "checkbox" || type === "radio") {
      emit({ t: 7, ts: now, id, c: Boolean(inputEl.checked) });
    } else if (typeof inputEl.value === "string") {
      const masked =
        isInsideMasked(target) || shouldMaskInput(target, Boolean(opts.maskAllInputs))
          ? maskValue(inputEl.value)
          : inputEl.value.slice(0, 512);
      emit({ t: 7, ts: now, id, v: masked });
    }
  };
  document.addEventListener("input", onInput, { capture: true, passive: true });
  document.addEventListener("change", onInput, { capture: true, passive: true });
  cleanups.push(() => {
    document.removeEventListener("input", onInput, { capture: true });
    document.removeEventListener("change", onInput, { capture: true });
  });

  // Resize & SPA Popstate / Hashchange
  let resizeTimer: ReturnType<typeof setTimeout> | null = null;
  const emitMeta = () => {
    lastHref = scrubUrl(location.href);
    emit({
      t: 1,
      ts: Date.now(),
      href: lastHref,
      w: Math.round(window.innerWidth || 1280),
      h: Math.round(window.innerHeight || 720),
      title: (document.title || "").slice(0, 120),
    });
  };
  const onResize = () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(emitMeta, 200);
  };
  window.addEventListener("resize", onResize, { passive: true });
  window.addEventListener("popstate", emitMeta);
  window.addEventListener("hashchange", emitMeta);
  cleanups.push(() => {
    if (resizeTimer) clearTimeout(resizeTimer);
    window.removeEventListener("resize", onResize);
    window.removeEventListener("popstate", emitMeta);
    window.removeEventListener("hashchange", emitMeta);
  });

  // Dynamic CSS-in-JS rule hook
  if (typeof CSSStyleSheet !== "undefined" && CSSStyleSheet.prototype?.insertRule) {
    const origInsertRule = CSSStyleSheet.prototype.insertRule;
    CSSStyleSheet.prototype.insertRule = function (rule: string, index?: number): number {
      const idx = origInsertRule.call(this, rule, index);
      try {
        if (!stopped && rule && rule.length < 8192) {
          const ownerId = this.ownerNode && serializerCtx ? serializerCtx.nodeToId.get(this.ownerNode) : undefined;
          emit({ t: 8, ts: Date.now(), id: ownerId, css: rule });
        }
      } catch {
        // never break host CSSOM
      }
      return idx;
    };
    cleanups.push(() => {
      CSSStyleSheet.prototype.insertRule = origInsertRule;
    });
  }

  // Periodic segment upload & lifecycle flush
  const flushInterval = setInterval(() => {
    if (Date.now() - lastActivityAt <= MAX_IDLE_MS) {
      flushSegment(false);
    }
  }, opts.flushIntervalMs ?? 5000);
  cleanups.push(() => clearInterval(flushInterval));

  const onPageHide = () => flushSegment(true);
  const onVisChange = () => {
    if (document.visibilityState === "hidden") {
      flushSegment(true);
    }
  };
  window.addEventListener("pagehide", onPageHide);
  document.addEventListener("visibilitychange", onVisChange);
  cleanups.push(() => {
    window.removeEventListener("pagehide", onPageHide);
    document.removeEventListener("visibilitychange", onVisChange);
  });

  // Ship initial full snapshot promptly (within 800ms) so short visits are captured too
  const initialFlushTimer = setTimeout(() => flushSegment(false), 800);
  cleanups.push(() => clearTimeout(initialFlushTimer));

  const ctrl: ReplayController = {
    flushNow(urgent = true) {
      flushSegment(urgent);
    },
    stop() {
      if (stopped) return;
      flushSegment(true);
      stopped = true;
      for (const fn of cleanups) {
        try {
          fn();
        } catch {
          // ignore
        }
      }
      if (win.__lumaEyeReplayCtrl === ctrl) {
        delete win.__lumaEyeReplayCtrl;
      }
    },
  };

  win.__lumaEyeReplayCtrl = ctrl;
  return ctrl;
}

if (typeof globalThis !== "undefined") {
  (globalThis as Record<string, unknown>).__LumaEyeReplay = {
    startSessionReplay,
    serializeDomTree,
    shouldMaskInput,
    maskValue,
  };
}
