export interface VitalSample {
  ts: number;
  name: string;
  value: number;
  rating: string;
  page_path: string;
}

function rateVital(name: string, val: number): string {
  const limits: Record<string, [number, number]> = {
    LCP: [2500, 4000],
    INP: [200, 500],
    CLS: [0.1, 0.25],
    FCP: [1800, 3000],
    TTFB: [800, 1800],
  };
  const pair = limits[name];
  if (!pair) return "good";
  if (val <= pair[0]) return "good";
  if (val <= pair[1]) return "needs-improvement";
  return "poor";
}

export function observeWebVitals(onVital: (v: VitalSample) => void): () => void {
  if (typeof PerformanceObserver === "undefined" || typeof performance === "undefined") {
    return () => {};
  }

  const observers: PerformanceObserver[] = [];
  const emitted: Record<string, number> = {};
  const path = () => (typeof location !== "undefined" ? location.pathname : "/");

  const emit = (name: string, val: number) => {
    const rounded = name === "CLS" ? Math.round(val * 1000) / 1000 : Math.round(val);
    if (emitted[name] === rounded) return;
    emitted[name] = rounded;
    onVital({
      ts: Date.now(),
      name,
      value: rounded,
      rating: rateVital(name, rounded),
      page_path: path(),
    });
  };

  // TTFB from navigation entry
  try {
    const navs = performance.getEntriesByType("navigation") as PerformanceNavigationTiming[];
    if (navs && navs.length > 0 && navs[0].responseStart > 0) {
      emit("TTFB", navs[0].responseStart);
    }
  } catch {
    // ignore
  }

  const observeType = (type: string, cb: (list: PerformanceObserverEntryList) => void, extra?: Record<string, unknown>) => {
    try {
      const obs = new PerformanceObserver(cb);
      obs.observe({ type, buffered: true, ...extra } as PerformanceObserverInit);
      observers.push(obs);
    } catch {
      // Unsupported entry type
    }
  };

  let latestLCP = 0;
  observeType("largest-contentful-paint", (list) => {
    const entries = list.getEntries();
    if (entries.length > 0) {
      latestLCP = entries[entries.length - 1].startTime;
    }
  });

  let clsValue = 0;
  observeType("layout-shift", (list) => {
    for (const entry of list.getEntries() as Array<PerformanceEntry & { hadRecentInput?: boolean; value?: number }>) {
      if (!entry.hadRecentInput && typeof entry.value === "number") {
        clsValue += entry.value;
      }
    }
  });

  observeType("paint", (list) => {
    for (const entry of list.getEntries()) {
      if (entry.name === "first-contentful-paint") {
        emit("FCP", entry.startTime);
      }
    }
  });

  let maxINP = 0;
  observeType("event", (list) => {
    for (const entry of list.getEntries() as Array<PerformanceEntry & { interactionId?: number }>) {
      if (entry.interactionId && entry.duration > maxINP) {
        maxINP = entry.duration;
      }
    }
  }, { durationThreshold: 40 });

  const flushSnapshot = () => {
    if (latestLCP > 0) emit("LCP", latestLCP);
    if (clsValue > 0) emit("CLS", clsValue);
    if (maxINP > 0) emit("INP", maxINP);
  };

  const onVis = () => {
    if (typeof document !== "undefined" && document.visibilityState === "hidden") {
      flushSnapshot();
    }
  };

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVis);
  }

  return () => {
    flushSnapshot();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVis);
    }
    for (const obs of observers) {
      try {
        obs.disconnect();
      } catch {
        // ignore
      }
    }
  };
}
