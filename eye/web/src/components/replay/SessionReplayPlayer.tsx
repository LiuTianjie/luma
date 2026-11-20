import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Activity,
  Globe,
  Maximize2,
  Minimize2,
  Monitor,
  Pause,
  Play,
  RotateCcw,
  RotateCw,
  ShieldCheck,
  TriangleAlert,
  Zap,
} from "@/components/icons";
import { api } from "@/api/client";
import type { Breadcrumb, TimelineItem, TimelineResponse } from "@/api/types";
import { DomReplayEngine, type CursorFrameState } from "@/components/replay/DomReplayEngine";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useQuery } from "@/hooks/useQuery";
import { cn } from "@/lib/utils";

export interface SeekCommand {
  offsetMs: number;
  seq: number;
  autoplay?: boolean;
}

interface SessionReplayPlayerProps {
  projectSlug: string;
  sessionId: string;
  timeline: TimelineResponse;
  refreshTick: number;
  seekCommand?: SeekCommand | null;
  onActiveOffsetChange?: (offsetMs: number) => void;
}

interface TimelineMarker {
  id: string;
  offsetMs: number;
  kind: "navigation" | "click" | "http_error" | "crash";
  label: string;
}

interface SchematicStep {
  id: string;
  offsetMs: number;
  kind: "navigation" | "click" | "custom" | "http" | "vital" | "log" | "crash";
  route: string;
  url: string;
  label: string;
  detail?: string;
  status?: number;
  isError?: boolean;
  xPct: number;
  yPct: number;
}

function formatClockMs(ms: number): string {
  const safe = Math.max(0, Math.floor(ms / 1000));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function parseFlexEpochMs(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw)) {
    return raw < 1e11 ? Math.round(raw * 1000) : Math.round(raw);
  }
  if (typeof raw === "string" && raw.trim()) {
    const asNum = Number(raw);
    if (Number.isFinite(asNum)) {
      return asNum < 1e11 ? Math.round(asNum * 1000) : Math.round(asNum);
    }
    const parsed = Date.parse(raw);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return null;
}

function extractPathFromUrl(raw?: string): string {
  if (!raw) return "/";
  if (raw.startsWith("/")) return raw.split("?")[0] || "/";
  try {
    return new URL(raw).pathname || "/";
  } catch {
    return raw;
  }
}

function deterministicTargetSlot(index: number): { xPct: number; yPct: number } {
  const slots = [
    { xPct: 28, yPct: 38 },
    { xPct: 68, yPct: 38 },
    { xPct: 32, yPct: 62 },
    { xPct: 68, yPct: 62 },
    { xPct: 50, yPct: 50 },
    { xPct: 50, yPct: 76 },
  ];
  return slots[index % slots.length];
}

function buildSchematicChoreography(items: TimelineItem[]): {
  steps: SchematicStep[];
  routes: string[];
  baseEpochMs: number;
  durationMs: number;
} {
  if (items.length === 0) {
    return { steps: [], routes: ["/"], baseEpochMs: Date.now(), durationMs: 5000 };
  }

  let minMs = items[0].ts_ms;
  let maxMs = items[items.length - 1].ts_ms;

  // Also check breadcrumb timestamps so pre-crash interactions before first pageview are included
  const rawSteps: Array<Omit<SchematicStep, "offsetMs" | "xPct" | "yPct"> & { tsMs: number; xPct?: number; yPct?: number }> = [];
  let latestUrl = items.find((it) => it.page_url)?.page_url || "";
  let latestRoute = items.find((it) => it.page_path)?.page_path || extractPathFromUrl(latestUrl);

  const seenBreadcrumbKeys = new Set<string>();

  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.page_url) latestUrl = it.page_url;
    if (it.page_path) latestRoute = it.page_path;
    else if (it.page_url) latestRoute = extractPathFromUrl(it.page_url);

    if (it.ts_ms < minMs) minMs = it.ts_ms;
    if (it.ts_ms > maxMs) maxMs = it.ts_ms;

    if (it.kind === "pageview") {
      rawSteps.push({
        id: it.id,
        tsMs: it.ts_ms,
        kind: "navigation",
        route: it.page_path || extractPathFromUrl(it.page_url) || latestRoute,
        url: it.page_url || latestUrl,
        label: `进入页面 ${it.page_path || it.title || "/"}`,
        xPct: 50,
        yPct: 24,
      });
    } else if (it.kind === "custom") {
      rawSteps.push({
        id: it.id,
        tsMs: it.ts_ms,
        kind: "custom",
        route: it.page_path || latestRoute,
        url: it.page_url || latestUrl,
        label: it.title,
        detail: it.props ? JSON.stringify(it.props).slice(0, 80) : undefined,
      });
    } else if (it.kind === "vital") {
      rawSteps.push({
        id: it.id,
        tsMs: it.ts_ms,
        kind: "vital",
        route: it.page_path || latestRoute,
        url: latestUrl,
        label: `${it.metric_name || it.title}: ${it.metric_name === "CLS" ? Number(it.metric_value || 0).toFixed(3) : `${Math.round(it.metric_value || 0)}ms`}`,
        isError: it.metric_rating === "poor",
        xPct: 82,
        yPct: 22,
      });
    } else if (it.kind === "log") {
      rawSteps.push({
        id: it.id,
        tsMs: it.ts_ms,
        kind: "log",
        route: extractPathFromUrl(it.page_url) || latestRoute,
        url: it.page_url || latestUrl,
        label: it.title,
        isError: it.level === "error" || it.level === "warn",
        xPct: 50,
        yPct: 78,
      });
    } else if (it.kind === "exception") {
      // Unpack 50-item RingBuffer breadcrumbs leading up to this crash!
      const bcs: Breadcrumb[] = it.breadcrumbs || [];
      for (let b = 0; b < bcs.length; b++) {
        const bc = bcs[b];
        const bcMs = parseFlexEpochMs(bc.ts) ?? it.ts_ms - (bcs.length - b) * 350;
        const dedupKey = `${bc.category}:${bcMs}:${bc.message}`;
        if (seenBreadcrumbKeys.has(dedupKey)) continue;
        seenBreadcrumbKeys.add(dedupKey);

        if (bcMs < minMs) minMs = bcMs;
        if (bcMs > maxMs) maxMs = bcMs;

        if (bc.category === "navigation") {
          const toPath = typeof bc.data?.to === "string" ? bc.data.to : bc.message.split("->").pop()?.trim() || latestRoute;
          rawSteps.push({
            id: `${it.id}-bc-${b}`,
            tsMs: bcMs,
            kind: "navigation",
            route: extractPathFromUrl(toPath),
            url: latestUrl,
            label: bc.message,
            xPct: 50,
            yPct: 24,
          });
        } else if (bc.category === "ui.click") {
          const xPct = typeof bc.data?.x_pct === "number" ? bc.data.x_pct : undefined;
          const yPct = typeof bc.data?.y_pct === "number" ? bc.data.y_pct : undefined;
          rawSteps.push({
            id: `${it.id}-bc-${b}`,
            tsMs: bcMs,
            kind: "click",
            route: latestRoute,
            url: it.page_url || latestUrl,
            label: bc.message,
            xPct,
            yPct,
          });
        } else if (bc.category === "http") {
          const status = typeof bc.data?.status === "number" ? bc.data.status : undefined;
          rawSteps.push({
            id: `${it.id}-bc-${b}`,
            tsMs: bcMs,
            kind: "http",
            route: latestRoute,
            url: it.page_url || latestUrl,
            label: bc.message,
            status,
            isError: bc.level === "error" || (status !== undefined && status >= 400),
            xPct: 50,
            yPct: 76,
          });
        } else if (bc.category === "console" && (bc.level === "error" || bc.level === "warn")) {
          rawSteps.push({
            id: `${it.id}-bc-${b}`,
            tsMs: bcMs,
            kind: "log",
            route: latestRoute,
            url: it.page_url || latestUrl,
            label: bc.message,
            isError: true,
            xPct: 50,
            yPct: 78,
          });
        }
      }

      rawSteps.push({
        id: it.id,
        tsMs: it.ts_ms,
        kind: "crash",
        route: extractPathFromUrl(it.page_url) || latestRoute,
        url: it.page_url || latestUrl,
        label: `${it.error_type || "Error"}: ${it.error_message || it.title}`,
        detail: it.culprit,
        isError: true,
        xPct: 50,
        yPct: 52,
      });
    }
  }

  rawSteps.sort((a, b) => a.tsMs - b.tsMs);

  // Propagate current route forward across steps and assign deterministic coordinates to clicks/customs
  const routesSet: string[] = [];
  let runningRoute = rawSteps.find((s) => s.route)?.route || "/";
  let slotCounter = 0;

  const steps: SchematicStep[] = rawSteps.map((s) => {
    if (s.kind === "navigation" && s.route) {
      runningRoute = s.route;
    }
    const route = s.route || runningRoute || "/";
    if (!routesSet.includes(route)) {
      routesSet.push(route);
    }
    const fallbackSlot = deterministicTargetSlot(slotCounter++);
    return {
      id: s.id,
      offsetMs: Math.max(0, s.tsMs - minMs),
      kind: s.kind,
      route,
      url: s.url,
      label: s.label,
      detail: s.detail,
      status: s.status,
      isError: s.isError,
      xPct: s.xPct ?? fallbackSlot.xPct,
      yPct: s.yPct ?? fallbackSlot.yPct,
    };
  });

  if (routesSet.length === 0) routesSet.push("/");
  const durationMs = Math.max(3000, maxMs - minMs + 800);
  return { steps, routes: routesSet, baseEpochMs: minMs, durationMs };
}

function CursorSvg() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className="drop-shadow-[0_2px_4px_rgba(0,0,0,0.42)]"
    >
      <path
        d="M4.2 2.6L19.6 11.4C20.4 11.85 20.15 13.08 19.23 13.19L12.7 13.97L9.55 19.86C9.12 20.67 7.9 20.38 7.88 19.46L4.02 3.55C3.84 2.81 4.55 2.23 4.2 2.6Z"
        fill="oklch(0.2 0.02 260)"
        stroke="white"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function SessionReplayPlayer({
  projectSlug,
  sessionId,
  timeline,
  refreshTick,
  seekCommand,
  onActiveOffsetChange,
}: SessionReplayPlayerProps) {
  const scrubberId = useId();
  const validSessionId = sessionId && !sessionId.startsWith("actor:") ? sessionId : "";
  const replayQueryKey =
    projectSlug && validSessionId ? `replay:${projectSlug}:${validSessionId}:${refreshTick}` : null;

  const { data: replayData, loading: replayLoading } = useQuery(replayQueryKey, (signal) =>
    api.replay(projectSlug, validSessionId, signal),
  );

  const hasDomReplay = Boolean(replayData?.has_dom_replay && replayData.events.length > 0);

  const domEngine = useMemo(() => {
    if (!replayData?.has_dom_replay || replayData.events.length === 0) return null;
    return new DomReplayEngine(
      replayData.events,
      replayData.viewport_w || 1280,
      replayData.viewport_h || 720,
      replayData.page_url || "",
    );
  }, [replayData]);

  const schematic = useMemo(() => buildSchematicChoreography(timeline.items || []), [timeline.items]);

  const baseStartMs = useMemo(() => {
    if (hasDomReplay && replayData && replayData.start_ms > 0) {
      return replayData.start_ms;
    }
    return schematic.baseEpochMs;
  }, [hasDomReplay, replayData, schematic.baseEpochMs]);

  const totalDurationMs = useMemo(() => {
    if (hasDomReplay && replayData) {
      const timelineSpan =
        timeline.items.length > 0
          ? Math.max(0, timeline.items[timeline.items.length - 1].ts_ms - replayData.start_ms)
          : 0;
      return Math.max(2000, replayData.duration_ms, timelineSpan);
    }
    return Math.max(2000, timeline.duration_ms || 0, schematic.durationMs);
  }, [hasDomReplay, replayData, timeline.duration_ms, timeline.items, schematic.durationMs]);

  // Unified markers for scrubber track & skip-idle gaps
  const { markers, activityOffsets, firstCrashOffsetMs } = useMemo(() => {
    const outMarkers: TimelineMarker[] = [];
    const offsets: number[] = [0];

    if (hasDomReplay && replayData) {
      for (let i = 0; i < replayData.events.length; i++) {
        const ev = replayData.events[i];
        const off = Math.max(0, ev.ts - baseStartMs);
        offsets.push(off);
        if (ev.t === 1) {
          outMarkers.push({
            id: `rp-nav-${i}`,
            offsetMs: off,
            kind: "navigation",
            label: `页面 ${extractPathFromUrl(ev.href)}`,
          });
        } else if (ev.t === 5) {
          outMarkers.push({
            id: `rp-clk-${i}`,
            offsetMs: off,
            kind: "click",
            label: ev.label ? `点击 "${ev.label}"` : `点击 ${ev.tag || "元素"}`,
          });
        }
      }
      for (let i = 0; i < timeline.items.length; i++) {
        const it = timeline.items[i];
        const off = Math.max(0, it.ts_ms - baseStartMs);
        offsets.push(off);
        if (it.kind === "exception") {
          outMarkers.push({
            id: `tl-exc-${i}`,
            offsetMs: off,
            kind: "crash",
            label: `${it.error_type || "Error"}: ${it.error_message || it.title}`,
          });
        }
      }
    } else {
      for (let i = 0; i < schematic.steps.length; i++) {
        const st = schematic.steps[i];
        offsets.push(st.offsetMs);
        if (st.kind === "navigation") {
          outMarkers.push({ id: st.id, offsetMs: st.offsetMs, kind: "navigation", label: st.label });
        } else if (st.kind === "click" || st.kind === "custom") {
          outMarkers.push({ id: st.id, offsetMs: st.offsetMs, kind: "click", label: st.label });
        } else if (st.kind === "http" && st.isError) {
          outMarkers.push({ id: st.id, offsetMs: st.offsetMs, kind: "http_error", label: st.label });
        } else if (st.kind === "crash") {
          outMarkers.push({ id: st.id, offsetMs: st.offsetMs, kind: "crash", label: st.label });
        }
      }
    }

    offsets.push(totalDurationMs);
    offsets.sort((a, b) => a - b);
    const crashMarker = outMarkers.find((m) => m.kind === "crash");
    return {
      markers: outMarkers,
      activityOffsets: offsets,
      firstCrashOffsetMs: crashMarker ? crashMarker.offsetMs : null,
    };
  }, [hasDomReplay, replayData, baseStartMs, timeline.items, schematic.steps, totalDurationMs]);

  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<1 | 2 | 4>(1);
  const [skipIdle, setSkipIdle] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [currentOffsetMs, setCurrentOffsetMs] = useState(0);
  const [skippedNotice, setSkippedNotice] = useState(false);
  const [domFrame, setDomFrame] = useState<{
    href: string;
    viewportW: number;
    viewportH: number;
    cursor: CursorFrameState;
  }>({
    href: replayData?.page_url || "",
    viewportW: replayData?.viewport_w || 1280,
    viewportH: replayData?.viewport_h || 720,
    cursor: { x: 0, y: 0, visible: false, clickTs: null },
  });

  const stageWrapRef = useRef<HTMLDivElement | null>(null);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const scrubberRef = useRef<HTMLInputElement | null>(null);
  const [stageBox, setStageBox] = useState({ w: 880, h: 480 });

  const offsetRef = useRef(0);
  const playingRef = useRef(playing);
  const speedRef = useRef(speed);
  const skipIdleRef = useRef(skipIdle);
  const lastReportTsRef = useRef(0);
  const onOffsetCbRef = useRef(onActiveOffsetChange);

  playingRef.current = playing;
  speedRef.current = speed;
  skipIdleRef.current = skipIdle;
  onOffsetCbRef.current = onActiveOffsetChange;

  // Sync CSS custom properties on `<input type="range">` per modern-web-guidance `slider`
  const syncScrubberCssVars = useCallback(
    (val: number) => {
      const el = scrubberRef.current;
      if (!el) return;
      el.style.setProperty("--attr-min", "0");
      el.style.setProperty("--attr-max", String(totalDurationMs));
      el.style.setProperty("--control-value", String(Math.min(totalDurationMs, Math.max(0, val))));
    },
    [totalDurationMs],
  );

  const applyFrameAt = useCallback(
    (nextOffset: number) => {
      const clamped = Math.max(0, Math.min(totalDurationMs, nextOffset));
      offsetRef.current = clamped;
      setCurrentOffsetMs(clamped);
      syncScrubberCssVars(clamped);

      if (domEngine && iframeRef.current) {
        const snap = domEngine.renderAt(iframeRef.current, clamped);
        setDomFrame(snap);
      }

      const now = performance.now();
      if (now - lastReportTsRef.current > 120 || clamped === 0 || clamped >= totalDurationMs) {
        lastReportTsRef.current = now;
        onOffsetCbRef.current?.(clamped);
      }
    },
    [domEngine, syncScrubberCssVars, totalDurationMs],
  );

  // Measure stage container for crisp proportional scaling of recorded viewport
  useEffect(() => {
    const el = stageWrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0 && rect.height > 0) {
        setStageBox({ w: rect.width, h: rect.height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [expanded]);

  // Reset & render initial frame when session or engine changes
  useEffect(() => {
    setPlaying(false);
    domEngine?.reset();
    // Defer one tick so iframe contentDocument is mounted
    const id = requestAnimationFrame(() => {
      applyFrameAt(0);
    });
    return () => cancelAnimationFrame(id);
  }, [validSessionId, domEngine, applyFrameAt]);

  // External seek requests (e.g. clicking a row in SessionDetailView timeline list)
  useEffect(() => {
    if (!seekCommand) return;
    applyFrameAt(seekCommand.offsetMs);
    if (seekCommand.autoplay !== undefined) {
      setPlaying(seekCommand.autoplay);
    }
  }, [seekCommand, applyFrameAt]);

  // 60fps requestAnimationFrame transport loop
  useEffect(() => {
    if (!playing) return;
    let rafId = 0;
    let prevTime = performance.now();
    let skipBadgeTimer: ReturnType<typeof setTimeout> | null = null;

    const tick = (now: number) => {
      const dt = Math.min(100, Math.max(0, now - prevTime));
      prevTime = now;
      let next = offsetRef.current + dt * speedRef.current;

      // Skip inactive gaps > 2500ms smoothly
      if (skipIdleRef.current && activityOffsets.length > 1) {
        for (let i = 0; i < activityOffsets.length; i++) {
          if (activityOffsets[i] > offsetRef.current) {
            const gapToNext = activityOffsets[i] - offsetRef.current;
            if (gapToNext > 2500 && activityOffsets[i] - 350 > next) {
              next = activityOffsets[i] - 350;
              setSkippedNotice(true);
              if (skipBadgeTimer) clearTimeout(skipBadgeTimer);
              skipBadgeTimer = setTimeout(() => setSkippedNotice(false), 900);
            }
            break;
          }
        }
      }

      if (next >= totalDurationMs) {
        applyFrameAt(totalDurationMs);
        setPlaying(false);
        return;
      }

      applyFrameAt(next);
      rafId = requestAnimationFrame(tick);
    };

    rafId = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(rafId);
      if (skipBadgeTimer) clearTimeout(skipBadgeTimer);
    };
  }, [playing, activityOffsets, totalDurationMs, applyFrameAt]);

  const togglePlay = () => {
    if (!playing && offsetRef.current >= totalDurationMs - 50) {
      applyFrameAt(0);
    }
    setPlaying((p) => !p);
  };

  // Compute current schematic state when Engine 2 is active
  const schematicState = useMemo(() => {
    if (hasDomReplay) return null;
    const steps = schematic.steps;
    let activeStep: SchematicStep | null = null;
    let nextStep: SchematicStep | null = null;
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].offsetMs <= currentOffsetMs) {
        activeStep = steps[i];
      } else {
        nextStep = steps[i];
        break;
      }
    }
    const currentRoute = activeStep?.route || schematic.routes[0] || "/";
    const currentUrl = activeStep?.url || currentRoute;

    // Collect interactive targets on the current route
    const routeTargets = steps.filter(
      (s) => s.route === currentRoute && (s.kind === "click" || s.kind === "custom"),
    );

    // Smooth cursor glide toward next click/custom target within 700ms window
    let cursorX = activeStep?.xPct ?? 50;
    let cursorY = activeStep?.yPct ?? 36;
    if (nextStep && (nextStep.kind === "click" || nextStep.kind === "custom" || nextStep.kind === "crash")) {
      const remain = nextStep.offsetMs - currentOffsetMs;
      if (remain > 0 && remain < 700) {
        const t = 1 - remain / 700;
        cursorX = cursorX + (nextStep.xPct - cursorX) * t;
        cursorY = cursorY + (nextStep.yPct - cursorY) * t;
      }
    }

    const recentLogsAndHttp = steps
      .filter(
        (s) =>
          s.offsetMs <= currentOffsetMs &&
          (s.kind === "http" || s.kind === "vital" || s.kind === "log" || s.kind === "crash"),
      )
      .slice(-3);

    const activeCrash =
      steps.find((s) => s.kind === "crash" && currentOffsetMs >= s.offsetMs && currentOffsetMs - s.offsetMs <= 4500) ||
      (currentOffsetMs >= totalDurationMs - 200 ? steps.find((s) => s.kind === "crash") : null);

    const isClickingNow =
      activeStep &&
      (activeStep.kind === "click" || activeStep.kind === "custom") &&
      currentOffsetMs - activeStep.offsetMs <= 480;

    return {
      currentRoute,
      currentUrl,
      activeStep,
      routeTargets,
      cursorX,
      cursorY,
      isClickingNow,
      recentLogsAndHttp,
      activeCrash,
    };
  }, [hasDomReplay, schematic, currentOffsetMs, totalDurationMs]);

  // Active crash banner in DOM replay mode when passing exception timestamp
  const domActiveCrash = useMemo(() => {
    if (!hasDomReplay) return null;
    const curEpoch = baseStartMs + currentOffsetMs;
    for (let i = timeline.items.length - 1; i >= 0; i--) {
      const it = timeline.items[i];
      if (it.kind === "exception" && curEpoch >= it.ts_ms && curEpoch - it.ts_ms <= 4500) {
        return `${it.error_type || "Error"}: ${it.error_message || it.title}`;
      }
    }
    return null;
  }, [hasDomReplay, baseStartMs, currentOffsetMs, timeline.items]);

  if (replayLoading && !replayData) {
    return <Skeleton className="h-[460px] w-full rounded-xl" />;
  }

  const vw = Math.max(320, domFrame.viewportW || 1280);
  const vh = Math.max(240, domFrame.viewportH || 720);
  const scale = Math.min(1, (stageBox.w - 16) / vw, (stageBox.h - 16) / vh);
  const scaledW = Math.round(vw * scale);
  const scaledH = Math.round(vh * scale);

  const liveUrlLabel = hasDomReplay
    ? domFrame.href || replayData?.page_url || "/"
    : schematicState?.currentUrl || "/";

  return (
    <Card className="overflow-hidden">
      {/* Top Browser & Mode Chrome */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline bg-muted/50 px-4 py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <div className="flex items-center gap-1.5" aria-hidden="true">
            <span className="size-2.5 rounded-full bg-border" />
            <span className="size-2.5 rounded-full bg-border" />
            <span className="size-2.5 rounded-full bg-border" />
          </div>

          <div className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1 font-mono text-xs text-muted-foreground">
            <Globe className="size-3.5 shrink-0 text-subtle-foreground" />
            <span className="truncate text-foreground">{liveUrlLabel}</span>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {hasDomReplay ? (
            <>
              <Badge variant="info">
                <Monitor className="mr-1 size-3" />
                1:1 DOM 精确还原 ({vw}×{vh})
              </Badge>
              <Badge variant="outline">
                <ShieldCheck className="mr-1 size-3" />
                沙盒隔离 · 敏感输入已打码
              </Badge>
            </>
          ) : (
            <Badge variant="secondary" title="接入 @nickname4th/luma-eye v1.3+ 后新会话将自动切换为 1:1 真实网页画面回放">
              <Activity className="mr-1 size-3" />
              事件轨迹舞台（历史会话兜底）
            </Badge>
          )}

          <Button
            type="button"
            variant="outline"
            size="icon-xs"
            aria-label={expanded ? "收起回放画布高度" : "展开回放画布高度"}
            title={expanded ? "收起画布" : "放大画布"}
            onClick={() => setExpanded((e) => !e)}
          >
            {expanded ? <Minimize2 /> : <Maximize2 />}
          </Button>
        </div>
      </div>

      {/* Viewport Stage */}
      <div
        ref={stageWrapRef}
        tabIndex={0}
        onKeyDown={(ev) => {
          if (ev.key === " " && ev.target === ev.currentTarget) {
            ev.preventDefault();
            togglePlay();
          }
        }}
        aria-label="用户会话回放舞台（按空格播放或暂停）"
        className={cn(
          "relative flex w-full items-center justify-center overflow-hidden bg-canvas select-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
          expanded ? "h-[640px]" : "h-[430px]",
        )}
      >
        {hasDomReplay ? (
          <div
            className="relative overflow-hidden rounded-md border border-border bg-white shadow-sm"
            style={{ width: `${scaledW}px`, height: `${scaledH}px` }}
          >
            <iframe
              ref={iframeRef}
              sandbox="allow-same-origin"
              title="会话 1:1 DOM 沙盒回放"
              className="pointer-events-none origin-top-left border-0 bg-white"
              style={{
                width: `${vw}px`,
                height: `${vh}px`,
                transform: `scale(${scale})`,
              }}
            />

            {/* Pointer Cursor & Click Ripple Overlay */}
            {domFrame.cursor.visible ? (
              <div
                className="pointer-events-none absolute top-0 left-0 z-20"
                style={{
                  transform: `translate(${Math.round(domFrame.cursor.x * scale)}px, ${Math.round(domFrame.cursor.y * scale)}px)`,
                }}
              >
                {domFrame.cursor.clickTs !== null ? (
                  <span
                    key={domFrame.cursor.clickTs}
                    className="animate-replay-click absolute top-0 left-0 size-8 rounded-full border-2 border-primary bg-primary/25"
                  />
                ) : null}
                <CursorSvg />
                {domFrame.cursor.clickLabel ? (
                  <span className="mt-1 ml-3 inline-block max-w-52 truncate rounded bg-foreground/90 px-2 py-0.5 font-mono text-[0.6875rem] text-background shadow-xs">
                    {domFrame.cursor.clickLabel}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : schematicState ? (
          /* Engine 2: Schematic Interactive Stage Fallback for historical / RN sessions */
          <div className="relative flex h-[calc(100%-24px)] w-[calc(100%-24px)] max-w-4xl flex-col justify-between overflow-hidden rounded-lg border border-border bg-card p-5 shadow-xs">
            {/* Route Flow Breadcrumb Strip */}
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[0.6875rem] font-medium text-muted-foreground">页面流转：</span>
                {schematic.routes.slice(0, 6).map((rt, idx) => {
                  const isCurrent = rt === schematicState.currentRoute;
                  return (
                    <span key={rt} className="inline-flex items-center gap-1.5">
                      {idx > 0 ? <span className="text-xs text-subtle-foreground">→</span> : null}
                      <span
                        className={cn(
                          "rounded px-2 py-0.5 font-mono text-xs transition-colors",
                          isCurrent
                            ? "bg-primary text-primary-foreground font-semibold"
                            : "bg-muted text-muted-foreground",
                        )}
                      >
                        {rt}
                      </span>
                    </span>
                  );
                })}
              </div>
              <span className="font-mono text-[0.6875rem] text-muted-foreground">
                {timeline.browser || "Browser"} · {timeline.os || "OS"}
              </span>
            </div>

            {/* Interactive Schematic Target Grid */}
            <div className="relative my-3 flex-1 rounded-md border border-dashed border-border bg-muted/30 p-4">
              {schematicState.routeTargets.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-1.5 text-center">
                  <span className="font-mono text-xs font-medium text-foreground">
                    当前视图：{schematicState.currentRoute}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {schematicState.activeStep?.label || "用户正在浏览当前页面内容"}
                  </span>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                  {schematicState.routeTargets.slice(0, 6).map((t) => {
                    const isActive = schematicState.activeStep?.id === t.id;
                    const isPassed = t.offsetMs <= currentOffsetMs;
                    return (
                      <div
                        key={t.id}
                        className={cn(
                          "flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs transition-all",
                          isActive
                            ? "border-primary bg-info-soft text-foreground ring-2 ring-primary/25"
                            : isPassed
                              ? "border-border bg-background text-foreground"
                              : "border-hairline bg-background/50 text-muted-foreground opacity-65",
                        )}
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <Zap className={cn("size-3.5 shrink-0", isActive ? "text-primary" : "text-muted-foreground")} />
                          <span className="truncate font-mono">{t.label}</span>
                        </div>
                        <span className="shrink-0 font-mono text-[0.6875rem] text-muted-foreground tabular-nums">
                          +{formatClockMs(t.offsetMs)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Animated Cursor on Schematic Stage */}
              <div
                className="pointer-events-none absolute z-20 transition-transform duration-150 ease-out"
                style={{
                  left: `${Math.min(92, Math.max(8, schematicState.cursorX))}%`,
                  top: `${Math.min(88, Math.max(12, schematicState.cursorY))}%`,
                }}
              >
                {schematicState.isClickingNow ? (
                  <span
                    key={schematicState.activeStep?.id}
                    className="animate-replay-click absolute top-0 left-0 size-8 rounded-full border-2 border-primary bg-primary/25"
                  />
                ) : null}
                <CursorSvg />
              </div>
            </div>

            {/* Bottom Live Network / Console / Vital Feed */}
            <div className="flex min-h-8 flex-wrap items-center justify-between gap-2 border-t border-hairline pt-2.5 text-xs">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                {schematicState.recentLogsAndHttp.length === 0 ? (
                  <span className="text-muted-foreground">暂无网络异常或控制台告警</span>
                ) : (
                  schematicState.recentLogsAndHttp.map((ev) => (
                    <Badge key={ev.id} variant={ev.isError ? "destructive" : "outline"} className="max-w-md truncate font-mono">
                      {ev.label}
                    </Badge>
                  ))
                )}
              </div>
              { !hasDomReplay ? (
                <span className="text-[0.6875rem] text-subtle-foreground">
                  升级 SDK 至 v1.3+ 后自动开启 1:1 真实网页回放
                </span>
              ) : null }
            </div>

            {schematicState.activeCrash ? (
              <div className="pointer-events-none absolute inset-x-4 bottom-12 z-30 flex items-start gap-2.5 rounded-lg border border-destructive bg-destructive-soft px-3.5 py-2.5 text-xs text-foreground shadow-sm">
                <TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
                <div className="min-w-0 flex-1">
                  <div className="font-semibold text-destructive-foreground">页面抛出未捕获异常</div>
                  <div className="truncate font-mono">{schematicState.activeCrash.label}</div>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Crash Overlay Toast for 1:1 DOM Mode */}
        {domActiveCrash ? (
          <div className="pointer-events-none absolute bottom-4 left-1/2 z-30 flex max-w-xl -translate-x-1/2 items-center gap-2 rounded-lg border border-destructive bg-destructive-soft px-3.5 py-2 text-xs shadow-sm">
            <TriangleAlert className="size-4 shrink-0 text-destructive" />
            <span className="truncate font-mono font-medium text-foreground">{domActiveCrash}</span>
          </div>
        ) : null}

        {/* Skip Inactive Indicator */}
        {skippedNotice ? (
          <div className="pointer-events-none absolute top-3 right-3 z-30 rounded-md border border-border bg-background/90 px-2.5 py-1 font-mono text-[0.6875rem] text-muted-foreground shadow-xs">
            已自动快进静止空闲时段
          </div>
        ) : null}
      </div>

      {/* Scrubber & Transport Controls */}
      <div className="flex flex-col gap-2 border-t border-hairline bg-background px-4 py-3">
        {/* Scrubber Track with Event Markers */}
        <div className="relative flex items-center">
          <label htmlFor={scrubberId} className="sr-only">
            会话回放时间进度条
          </label>
          <input
            ref={scrubberRef}
            id={scrubberId}
            type="range"
            className="replay-scrubber relative z-10"
            min={0}
            max={totalDurationMs}
            step={20}
            value={Math.round(currentOffsetMs)}
            aria-valuetext={`${formatClockMs(currentOffsetMs)} / ${formatClockMs(totalDurationMs)}`}
            onChange={(ev) => {
              setPlaying(false);
              applyFrameAt(Number(ev.target.value));
            }}
          />

          {/* Color-coded event pin ticks along scrubber */}
          <div className="pointer-events-none absolute inset-x-1.5 top-1/2 h-3 -translate-y-1/2" aria-hidden="true">
            {markers.slice(0, 80).map((m) => {
              const pct = Math.min(100, Math.max(0, (m.offsetMs / Math.max(1, totalDurationMs)) * 100));
              return (
                <span
                  key={m.id}
                  style={{ left: `${pct}%` }}
                  className={cn(
                    "absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full",
                    m.kind === "crash"
                      ? "z-20 h-3 w-1 bg-destructive"
                      : m.kind === "http_error"
                        ? "h-2.5 w-0.5 bg-warning"
                        : m.kind === "navigation"
                          ? "h-2 w-0.5 bg-info"
                          : "h-1.5 w-0.5 bg-foreground/35",
                  )}
                />
              );
            })}
          </div>
        </div>

        {/* Transport Bar */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              type="button"
              size="xs"
              variant="default"
              onClick={togglePlay}
              aria-label={playing ? "暂停回放" : "播放回放"}
            >
              {playing ? <Pause data-icon="inline-start" /> : <Play data-icon="inline-start" />}
              {playing ? "暂停" : currentOffsetMs >= totalDurationMs - 50 ? "重播" : "播放"}
            </Button>

            <Button
              type="button"
              size="icon-xs"
              variant="outline"
              title="后退 5 秒"
              aria-label="后退 5 秒"
              onClick={() => applyFrameAt(currentOffsetMs - 5000)}
            >
              <RotateCcw />
            </Button>

            <Button
              type="button"
              size="icon-xs"
              variant="outline"
              title="前进 5 秒"
              aria-label="前进 5 秒"
              onClick={() => applyFrameAt(currentOffsetMs + 5000)}
            >
              <RotateCw />
            </Button>

            <span className="ml-1.5 font-mono text-xs text-foreground tabular-nums">
              {formatClockMs(currentOffsetMs)}{" "}
              <span className="text-muted-foreground">/ {formatClockMs(totalDurationMs)}</span>
            </span>

            {firstCrashOffsetMs !== null ? (
              <Button
                type="button"
                size="xs"
                variant="outline"
                className="ml-1 text-destructive-foreground"
                onClick={() => {
                  applyFrameAt(Math.max(0, firstCrashOffsetMs - 3000));
                  setPlaying(true);
                }}
              >
                <TriangleAlert data-icon="inline-start" className="text-destructive" />
                跳至异常前 3 秒
              </Button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="xs"
              variant={skipIdle ? "secondary" : "outline"}
              aria-pressed={skipIdle}
              onClick={() => setSkipIdle((s) => !s)}
            >
              跳过空闲
            </Button>

            <div className="inline-flex rounded-md ring-1 ring-border" role="group" aria-label="回放倍速">
              {([1, 2, 4] as const).map((sp) => (
                <Button
                  key={sp}
                  type="button"
                  size="xs"
                  variant={speed === sp ? "secondary" : "ghost"}
                  aria-pressed={speed === sp}
                  className="rounded-none font-mono first:rounded-l-md last:rounded-r-md"
                  onClick={() => setSpeed(sp)}
                >
                  {sp}x
                </Button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
}
