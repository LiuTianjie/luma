import type { ReplayEvent, SerializedDomNode } from "@/api/types";

const SVG_NS = "http://www.w3.org/2000/svg";
const FORBIDDEN_TAGS = new Set(["script", "noscript", "applet", "object", "embed", "meta"]);

export interface CursorFrameState {
  x: number;
  y: number;
  visible: boolean;
  clickTs: number | null;
  clickLabel?: string;
}

export interface DomReplaySnapshotState {
  href: string;
  viewportW: number;
  viewportH: number;
  cursor: CursorFrameState;
}

interface PointerKeyframe {
  ts: number;
  x: number;
  y: number;
}

interface ClickKeyframe {
  ts: number;
  x: number;
  y: number;
  label?: string;
}

export class DomReplayEngine {
  private events: ReplayEvent[];
  private startMs: number;
  private idToNode = new Map<number, Node>();
  private appliedIndex = -1;
  private currentHref = "";
  private viewportW = 1280;
  private viewportH = 720;
  private dynamicStyleEl: HTMLStyleElement | null = null;

  private pointerFrames: PointerKeyframe[] = [];
  private clickFrames: ClickKeyframe[] = [];
  private snapshotIndices: number[] = [];

  constructor(events: ReplayEvent[], fallbackViewportW = 1280, fallbackViewportH = 720, fallbackUrl = "") {
    this.events = events;
    this.startMs = events.length > 0 ? events[0].ts : 0;
    this.viewportW = fallbackViewportW || 1280;
    this.viewportH = fallbackViewportH || 720;
    this.currentHref = fallbackUrl;

    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      if (ev.t === 2) {
        this.snapshotIndices.push(i);
        if (ev.w > 0) this.viewportW = ev.w;
        if (ev.h > 0) this.viewportH = ev.h;
        if (ev.href) this.currentHref = ev.href;
      } else if (ev.t === 4) {
        for (let k = 0; k < ev.pts.length; k++) {
          const [x, y, dt] = ev.pts[k];
          this.pointerFrames.push({ ts: ev.ts + dt, x, y });
        }
      } else if (ev.t === 5) {
        this.pointerFrames.push({ ts: ev.ts, x: ev.x, y: ev.y });
        this.clickFrames.push({
          ts: ev.ts,
          x: ev.x,
          y: ev.y,
          label: ev.label ? `${ev.tag || "click"} "${ev.label}"` : ev.tag,
        });
      }
    }

    this.pointerFrames.sort((a, b) => a.ts - b.ts);
    this.clickFrames.sort((a, b) => a.ts - b.ts);
  }

  getStartMs(): number {
    return this.startMs;
  }

  reset(): void {
    this.appliedIndex = -1;
    this.idToNode.clear();
    this.dynamicStyleEl = null;
  }

  /**
   * Synchronizes the sandboxed iframe document and returns live viewport + cursor state at `offsetMs`.
   */
  renderAt(iframe: HTMLIFrameElement | null, offsetMs: number): DomReplaySnapshotState {
    const targetTs = this.startMs + Math.max(0, offsetMs);
    const doc = iframe?.contentDocument || null;

    if (doc && this.events.length > 0) {
      // Find target event index <= targetTs (always apply at least initial snapshot so frame 0 isn't blank)
      let targetIdx = -1;
      for (let i = 0; i < this.events.length; i++) {
        if (this.events[i].ts <= targetTs) {
          targetIdx = i;
        } else {
          break;
        }
      }
      if (targetIdx < 0 && this.snapshotIndices.length > 0) {
        targetIdx = this.snapshotIndices[0];
      }

      // If seeking backwards or uninitialized, rebuild from nearest FullSnapshot (t === 2) <= targetIdx
      if (targetIdx < this.appliedIndex || this.appliedIndex < 0) {
        let snapIdx = this.snapshotIndices.length > 0 ? this.snapshotIndices[0] : 0;
        for (let s = 0; s < this.snapshotIndices.length; s++) {
          if (this.snapshotIndices[s] <= targetIdx) {
            snapIdx = this.snapshotIndices[s];
          } else {
            break;
          }
        }
        this.idToNode.clear();
        this.dynamicStyleEl = null;
        this.appliedIndex = -1;
        // Apply any leading meta event before snapIdx
        for (let i = 0; i <= Math.max(targetIdx, snapIdx); i++) {
          if (i < snapIdx && this.events[i].t !== 1) continue;
          this.applyEvent(doc, this.events[i]);
          this.appliedIndex = i;
        }
      } else if (targetIdx > this.appliedIndex) {
        for (let i = this.appliedIndex + 1; i <= targetIdx; i++) {
          this.applyEvent(doc, this.events[i]);
          this.appliedIndex = i;
        }
      }
    }

    return {
      href: this.currentHref,
      viewportW: this.viewportW,
      viewportH: this.viewportH,
      cursor: this.computeCursorAt(targetTs),
    };
  }

  private applyEvent(doc: Document, ev: ReplayEvent): void {
    switch (ev.t) {
      case 1: {
        if (ev.href) this.currentHref = ev.href;
        if (ev.w > 0) this.viewportW = ev.w;
        if (ev.h > 0) this.viewportH = ev.h;
        break;
      }
      case 2: {
        if (ev.href) this.currentHref = ev.href;
        if (ev.w > 0) this.viewportW = ev.w;
        if (ev.h > 0) this.viewportH = ev.h;
        this.rebuildFullDocument(doc, ev.root, ev.href);
        break;
      }
      case 3: {
        if (ev.rm) {
          for (let i = 0; i < ev.rm.length; i++) {
            const node = this.idToNode.get(ev.rm[i]);
            if (node && node.parentNode) {
              node.parentNode.removeChild(node);
            }
            this.idToNode.delete(ev.rm[i]);
          }
        }
        if (ev.ad) {
          for (let i = 0; i < ev.ad.length; i++) {
            const { p, nx, n } = ev.ad[i];
            const parent = this.idToNode.get(p);
            if (!parent) continue;
            const inSvg =
              parent.nodeType === 1 &&
              ((parent as Element).namespaceURI === SVG_NS || (parent as Element).tagName.toLowerCase() === "svg");
            const built = this.buildNode(doc, n, inSvg);
            if (!built) continue;
            const nextSibling = nx !== null ? this.idToNode.get(nx) || null : null;
            try {
              if (nextSibling && nextSibling.parentNode === parent) {
                parent.insertBefore(built, nextSibling);
              } else {
                parent.appendChild(built);
              }
            } catch {
              // ignore invalid hierarchy edge cases
            }
          }
        }
        if (ev.tx) {
          for (let i = 0; i < ev.tx.length; i++) {
            const node = this.idToNode.get(ev.tx[i].id);
            if (node) node.nodeValue = ev.tx[i].v;
          }
        }
        if (ev.at) {
          for (let i = 0; i < ev.at.length; i++) {
            const { id, k, v } = ev.at[i];
            const node = this.idToNode.get(id);
            if (!node || node.nodeType !== 1) continue;
            this.setSafeAttribute(node as Element, k, v);
          }
        }
        break;
      }
      case 6: {
        if (ev.id === 0) {
          doc.defaultView?.scrollTo(ev.x, ev.y);
          if (doc.documentElement) {
            doc.documentElement.scrollLeft = ev.x;
            doc.documentElement.scrollTop = ev.y;
          }
        } else {
          const target = this.idToNode.get(ev.id);
          if (target && target.nodeType === 1) {
            (target as HTMLElement).scrollLeft = ev.x;
            (target as HTMLElement).scrollTop = ev.y;
          }
        }
        break;
      }
      case 7: {
        const target = this.idToNode.get(ev.id);
        if (target && target.nodeType === 1) {
          const el = target as HTMLInputElement;
          if (ev.v !== undefined) {
            el.value = ev.v;
            el.setAttribute("value", ev.v);
          }
          if (ev.c !== undefined) {
            el.checked = ev.c;
          }
        }
        break;
      }
      case 8: {
        if (!ev.css) break;
        if (!this.dynamicStyleEl && doc.head) {
          const st = doc.createElement("style");
          st.setAttribute("data-replay-dynamic", "1");
          doc.head.appendChild(st);
          this.dynamicStyleEl = st;
        }
        if (this.dynamicStyleEl) {
          this.dynamicStyleEl.appendChild(doc.createTextNode(`${ev.css}\n`));
        }
        break;
      }
    }
  }

  private rebuildFullDocument(doc: Document, root: SerializedDomNode, baseHref: string): void {
    this.idToNode.clear();
    this.dynamicStyleEl = null;
    this.idToNode.set(root.id, doc);

    while (doc.firstChild) {
      doc.removeChild(doc.firstChild);
    }

    let htmlSerialized: SerializedDomNode | null = null;
    if (root.ch) {
      for (let i = 0; i < root.ch.length; i++) {
        const c = root.ch[i];
        if (c.t === 1) {
          const dt = doc.implementation.createDocumentType(c.tag || "html", "", "");
          this.idToNode.set(c.id, dt);
          doc.appendChild(dt);
        } else if (c.t === 2 && c.tag === "html") {
          htmlSerialized = c;
        }
      }
    }

    if (!htmlSerialized) {
      const fallbackHtml = doc.createElement("html");
      const fallbackBody = doc.createElement("body");
      fallbackHtml.appendChild(fallbackBody);
      doc.appendChild(fallbackHtml);
      return;
    }

    const htmlEl = this.buildNode(doc, htmlSerialized, false) as HTMLHtmlElement | null;
    if (!htmlEl) return;
    doc.appendChild(htmlEl);

    let headEl = htmlEl.querySelector("head");
    if (!headEl) {
      headEl = doc.createElement("head");
      htmlEl.insertBefore(headEl, htmlEl.firstChild);
    }

    // Defense-in-depth CSP inside sandboxed iframe: strictly forbid scripts & plugins
    const cspMeta = doc.createElement("meta");
    cspMeta.setAttribute("http-equiv", "Content-Security-Policy");
    cspMeta.setAttribute("content", "script-src 'none'; object-src 'none';");
    headEl.insertBefore(cspMeta, headEl.firstChild);

    if (baseHref && /^https?:\/\//i.test(baseHref)) {
      const baseEl = doc.createElement("base");
      baseEl.setAttribute("href", baseHref);
      headEl.insertBefore(baseEl, cspMeta.nextSibling);
    }

    // Inject helper styles for [data-eye-block] & media placeholders without overriding host page styles
    const helperStyle = doc.createElement("style");
    helperStyle.textContent = `
      [data-luma-blocked="1"] {
        background: repeating-linear-gradient(-45deg, rgba(148,163,184,0.16), rgba(148,163,184,0.16) 8px, rgba(148,163,184,0.28) 8px, rgba(148,163,184,0.28) 16px) !important;
        border: 1px dashed rgba(100,116,139,0.45) !important;
        border-radius: 6px !important;
      }
      * { pointer-events: none !important; user-select: none !important; }
    `;
    headEl.appendChild(helperStyle);
  }

  private buildNode(doc: Document, sn: SerializedDomNode, inSvg: boolean): Node | null {
    if (sn.t === 3) {
      const tn = doc.createTextNode(sn.tx ?? "");
      this.idToNode.set(sn.id, tn);
      return tn;
    }

    if (sn.t !== 2 || !sn.tag) return null;
    const tag = sn.tag.toLowerCase();
    if (FORBIDDEN_TAGS.has(tag)) return null;

    // If <link rel="stylesheet"> already has inlined CSS rules, emit a <style> directly so deploy hash rotations never break styles!
    if (tag === "link" && sn.css) {
      const styleEl = doc.createElement("style");
      styleEl.setAttribute("data-inlined-href", sn.a?.href || "");
      styleEl.textContent = sn.css;
      this.idToNode.set(sn.id, styleEl);
      return styleEl;
    }

    const isSvg = inSvg || Boolean(sn.svg) || tag === "svg";
    let el: Element;
    try {
      el = isSvg ? doc.createElementNS(SVG_NS, tag) : doc.createElement(tag);
    } catch {
      el = doc.createElement("div");
    }
    this.idToNode.set(sn.id, el);

    if (sn.blocked) {
      const [w, h] = sn.blocked;
      el.setAttribute("data-luma-blocked", "1");
      (el as HTMLElement).style.width = `${w}px`;
      (el as HTMLElement).style.height = `${h}px`;
      return el;
    }

    if (sn.a) {
      const entries = Object.entries(sn.a);
      for (let i = 0; i < entries.length; i++) {
        const [k, v] = entries[i];
        if (k === "_val") {
          (el as HTMLInputElement).value = v;
          if (tag === "input") el.setAttribute("value", v);
        } else if (k === "_chk") {
          (el as HTMLInputElement).checked = v === "1";
        } else {
          this.setSafeAttribute(el, k, v);
        }
      }
    }

    if (sn.box && (tag === "canvas" || tag === "video" || tag === "iframe")) {
      const [w, h] = sn.box;
      if (tag === "iframe") {
        const placeholder = doc.createElement("div");
        placeholder.setAttribute("data-luma-blocked", "1");
        placeholder.style.width = `${w}px`;
        placeholder.style.height = `${h}px`;
        this.idToNode.set(sn.id, placeholder);
        return placeholder;
      }
      (el as HTMLElement).style.minWidth = `${w}px`;
      (el as HTMLElement).style.minHeight = `${h}px`;
    }

    if (tag === "style" && sn.css) {
      el.textContent = sn.css;
      return el;
    }

    if (sn.ch) {
      for (let i = 0; i < sn.ch.length; i++) {
        const child = this.buildNode(doc, sn.ch[i], isSvg);
        if (child) el.appendChild(child);
      }
    }

    return el;
  }

  private setSafeAttribute(el: Element, rawKey: string, val: string | null): void {
    const k = rawKey.trim();
    const lk = k.toLowerCase();
    if (!k || lk.startsWith("on") || lk === "srcdoc" || lk === "nonce" || lk === "integrity") {
      return;
    }
    if (val === null) {
      el.removeAttribute(k);
      return;
    }
    if ((lk === "href" || lk === "src" || lk === "action" || lk === "xlink:href") && /^\s*javascript:/i.test(val)) {
      return;
    }
    try {
      el.setAttribute(k, val);
    } catch {
      // ignore invalid XML/HTML attribute names
    }
  }

  private computeCursorAt(ts: number): CursorFrameState {
    let activeClick: ClickKeyframe | null = null;
    for (let i = this.clickFrames.length - 1; i >= 0; i--) {
      const cf = this.clickFrames[i];
      if (cf.ts <= ts) {
        if (ts - cf.ts <= 500) {
          activeClick = cf;
        }
        break;
      }
    }

    if (this.pointerFrames.length === 0) {
      return { x: 0, y: 0, visible: false, clickTs: null };
    }

    // Binary search pointerFrames for smooth linear interpolation
    let lo = 0;
    let hi = this.pointerFrames.length - 1;
    if (ts <= this.pointerFrames[0].ts) {
      const p0 = this.pointerFrames[0];
      return {
        x: p0.x,
        y: p0.y,
        visible: true,
        clickTs: activeClick?.ts ?? null,
        clickLabel: activeClick?.label,
      };
    }
    if (ts >= this.pointerFrames[hi].ts) {
      const pLast = this.pointerFrames[hi];
      return {
        x: pLast.x,
        y: pLast.y,
        visible: true,
        clickTs: activeClick?.ts ?? null,
        clickLabel: activeClick?.label,
      };
    }

    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.pointerFrames[mid].ts <= ts) {
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }

    const prev = this.pointerFrames[Math.max(0, hi)];
    const next = this.pointerFrames[Math.min(this.pointerFrames.length - 1, hi + 1)];
    const gap = next.ts - prev.ts;
    if (gap <= 0 || gap > 1200) {
      return {
        x: prev.x,
        y: prev.y,
        visible: true,
        clickTs: activeClick?.ts ?? null,
        clickLabel: activeClick?.label,
      };
    }
    const alpha = Math.max(0, Math.min(1, (ts - prev.ts) / gap));
    return {
      x: prev.x + (next.x - prev.x) * alpha,
      y: prev.y + (next.y - prev.y) * alpha,
      visible: true,
      clickTs: activeClick?.ts ?? null,
      clickLabel: activeClick?.label,
    };
  }
}
