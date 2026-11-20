import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import {
  createLumaEye,
  DEFAULT_SESSION_IDLE_TIMEOUT_MS,
  DEFAULT_SESSION_MAX_DURATION_MS,
  getTotalForegroundTime,
  RingBuffer,
  SessionManager,
} from "../dist/index.mjs";

function makeMemoryStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    _map: m,
  };
}

let savedGlobals;

beforeEach(() => {
  savedGlobals = {
    localStorage: globalThis.localStorage,
    sessionStorage: globalThis.sessionStorage,
    fetchLater: globalThis.fetchLater,
    fetch: globalThis.fetch,
    navigator: globalThis.navigator,
    document: globalThis.document,
    performance: globalThis.performance,
    Request: globalThis.Request,
  };
  globalThis.localStorage = makeMemoryStorage();
  globalThis.sessionStorage = makeMemoryStorage();
});

afterEach(() => {
  for (const [k, v] of Object.entries(savedGlobals)) {
    if (v === undefined) {
      delete globalThis[k];
    } else {
      Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
    }
  }
});

test("RingBuffer overwrites oldest items at fixed capacity", () => {
  const rb = new RingBuffer(50);
  for (let i = 0; i < 65; i++) {
    rb.push({ ts: i, category: "test", level: "info", message: `msg-${i}` });
  }
  const arr = rb.toArray();
  assert.equal(arr.length, 50);
  assert.equal(arr[0].message, "msg-15");
  assert.equal(arr[49].message, "msg-64");
});

test("Dual-Track separation & fetchLater coalescing", () => {
  const fetchLaterCalls = [];
  const beacons = [];

  globalThis.fetchLater = (url, init) => {
    fetchLaterCalls.push({ url, init, aborted: false });
    const entry = fetchLaterCalls[fetchLaterCalls.length - 1];
    init.signal?.addEventListener("abort", () => {
      entry.aborted = true;
    });
    return { activated: false };
  };
  Object.defineProperty(globalThis, "navigator", {
    value: {
      sendBeacon: (url, body) => {
        beacons.push({ url, body });
        return true;
      },
    },
    configurable: true,
    writable: true,
  });

  const eye = createLumaEye({
    endpoint: "https://eye.example.com",
    key: "pk_demo_test",
    project: "demo-app",
    batchWindowMs: 3000,
  });

  // Silent debug logs & breadcrumbs stay ONLY in Ring Buffer (0 network calls!)
  eye.addBreadcrumb("ui.click", "button#scan");
  eye.log("debug", "token check?token=supersecret");
  assert.equal(fetchLaterCalls.length, 0);
  assert.equal(beacons.length, 0);

  // Track 1 events coalesce via fetchLater + AbortController
  eye.track("upload_code", { lang: "ts" });
  eye.track("scan_start", { files: 12 });
  assert.equal(fetchLaterCalls.length, 2);
  assert.equal(fetchLaterCalls[0].aborted, true);
  assert.equal(fetchLaterCalls[1].aborted, false);
  assert.equal(fetchLaterCalls[1].init.priority, "low");
  assert.equal(fetchLaterCalls[1].init.headers, undefined);
  const track1Body = JSON.parse(fetchLaterCalls[1].init.body);
  assert.equal(track1Body.track, "normal");
  assert.equal(track1Body.events.length, 2);

  // Track 2 urgent crash attaches full Ring Buffer breadcrumbs and dispatches immediately
  eye.captureError(new TypeError("boom"));
  assert.equal(beacons.length, 1);
  const urgentBody = JSON.parse(beacons[0].body);
  assert.equal(urgentBody.track, "urgent");
  assert.equal(urgentBody.errors.length, 1);
  assert.equal(urgentBody.errors[0].breadcrumbs.length, 3);
  assert.equal(urgentBody.errors[0].breadcrumbs[0].message, "button#scan");
  assert.equal(urgentBody.errors[0].breadcrumbs[1].message, "token check?token=supersecret");
  assert.equal(urgentBody.errors[0].breadcrumbs[2].category, "exception");

  eye.destroy();
});

test("Crash Outbox persists failed crash and drains on next boot", () => {
  const sent = [];
  let failTransport = true;

  Object.defineProperty(globalThis, "navigator", {
    value: {
      sendBeacon: (url, body) => {
        if (failTransport) return false;
        sent.push({ url, body });
        return true;
      },
    },
    configurable: true,
    writable: true,
  });
  globalThis.fetch = async () => {
    throw new Error("offline");
  };
  globalThis.fetchLater = () => ({ activated: false });

  const eye1 = createLumaEye({
    endpoint: "https://eye.example.com",
    key: "pk_test",
    project: "outboxproj",
  });
  eye1.addBreadcrumb("navigation", "/ -> /checkout");
  // Simulate unavailable beacon & fetch
  globalThis.fetch = undefined;
  eye1.captureError(new Error("crash during unload"));
  assert.equal(sent.length, 0);
  const rawOutbox = globalThis.localStorage.getItem("__luma_eye_outbox_outboxproj__");
  assert.ok(rawOutbox && rawOutbox.includes("crash during unload"));
  eye1.destroy();

  // Next boot recovers and drains the Crash Outbox!
  failTransport = false;
  const eye2 = createLumaEye({
    endpoint: "https://eye.example.com",
    key: "pk_test",
    project: "outboxproj",
  });
  assert.equal(sent.length, 1);
  assert.equal(globalThis.localStorage.getItem("__luma_eye_outbox_outboxproj__"), null);
  eye2.destroy();
});

test("Client Error Storm circuit breaker caps urgent dispatches per page", () => {
  const beacons = [];
  Object.defineProperty(globalThis, "navigator", {
    value: {
      sendBeacon: (url, body) => {
        beacons.push({ url, body });
        return true;
      },
    },
    configurable: true,
    writable: true,
  });
  globalThis.fetchLater = () => ({ activated: false });

  const eye = createLumaEye({
    endpoint: "https://eye.example.com",
    key: "pk_test",
    project: "stormproj",
    maxErrorsPerPage: 20,
  });
  for (let i = 0; i < 30; i++) {
    eye.captureError(new Error(`loop-${i}`));
  }
  assert.equal(beacons.length, 20);
  eye.destroy();
});

test("getTotalForegroundTime computes visible intervals and falls back cleanly", () => {
  globalThis.performance = {
    now: () => 5000,
    getEntriesByType: (type) => {
      assert.equal(type, "visibility-state");
      return [
        { name: "visible", startTime: 0 },
        { name: "hidden", startTime: 1200 },
        { name: "visible", startTime: 3000 },
      ];
    },
  };
  assert.equal(getTotalForegroundTime(), 3200);

  globalThis.performance = {
    now: () => 4321,
    getEntriesByType: () => [],
  };
  assert.equal(getTotalForegroundTime(), 4321);
});

test("Industry-standard Session: cross-tab localStorage sharing, 30m sliding idle timeout, and 24h max cap", () => {
  assert.equal(DEFAULT_SESSION_IDLE_TIMEOUT_MS, 30 * 60 * 1000);
  assert.equal(DEFAULT_SESSION_MAX_DURATION_MS, 24 * 60 * 60 * 1000);

  const sharedLocal = makeMemoryStorage();
  let fakeNow = 1_700_000_000_000;
  const origNow = Date.now;
  Date.now = () => fakeNow;

  try {
    globalThis.localStorage = sharedLocal;
    globalThis.fetchLater = () => ({ activated: false });

    // Tab 1 and Tab 2 have completely separate sessionStorage, but share localStorage
    globalThis.sessionStorage = makeMemoryStorage();
    const tab1 = createLumaEye({
      endpoint: "https://eye.example.com",
      key: "pk_sess",
      project: "itool",
      autoPageviews: false,
      autoVitals: false,
      autoErrors: false,
    });
    const sidInitial = tab1.getSessionId();
    const uidInitial = tab1.getActorId();
    assert.ok(sidInitial.startsWith("s_"));
    assert.ok(uidInitial.startsWith("u_"));

    // Open Tab 2 5 minutes later -> shares exact same actor_id AND session_id!
    fakeNow += 5 * 60 * 1000;
    globalThis.sessionStorage = makeMemoryStorage();
    const tab2 = createLumaEye({
      endpoint: "https://eye.example.com",
      key: "pk_sess",
      project: "itool",
      autoPageviews: false,
      autoVitals: false,
      autoErrors: false,
    });
    assert.equal(tab2.getActorId(), uidInitial);
    assert.equal(tab2.getSessionId(), sidInitial);

    // Sliding window: +20m activity in Tab 2, then +20m activity in Tab 1 (total 45m elapsed, never idle >30m)
    fakeNow += 20 * 60 * 1000;
    assert.equal(tab2.getSessionId(), sidInitial);
    fakeNow += 20 * 60 * 1000;
    assert.equal(tab1.getSessionId(), sidInitial);

    // Idle > 30 minutes -> rotates to a new session_id while keeping same actor_id
    fakeNow += 31 * 60 * 1000;
    const sidAfterIdle = tab1.getSessionId();
    assert.notEqual(sidAfterIdle, sidInitial);
    assert.equal(tab1.getActorId(), uidInitial);

    // Tab 2 picks up Tab 1's rotated session_id automatically
    fakeNow += 60 * 1000;
    assert.equal(tab2.getSessionId(), sidAfterIdle);

    // Continuous activity exceeding 24h hard cap rotates session even without >30m idle gap
    const sm = new SessionManager({
      project: "captest",
      storage: makeMemoryStorage(),
      now: () => fakeNow,
    });
    const capSid1 = sm.getSessionId();
    for (let i = 0; i < 60; i++) {
      fakeNow += 25 * 60 * 1000; // 60 * 25m = 25 hours total, never >30m idle
      sm.touch();
    }
    const capSid2 = sm.getSessionId();
    assert.notEqual(capSid2, capSid1);

    tab1.destroy();
    tab2.destroy();
  } finally {
    Date.now = origNow;
  }
});

test("Cross-subdomain root cookie shares actor_id and session_id across apex and www origins", () => {
  // Simulate shared browser cookie jar for *.example.com
  const jar = new Map();
  const makeDocWithCookieJar = (hostname) => ({
    referrer: "",
    visibilityState: "visible",
    addEventListener() {},
    removeEventListener() {},
    get cookie() {
      const pairs = [];
      for (const [k, entry] of jar.entries()) {
        if (entry.maxAge <= 0) continue;
        if (!entry.domain || hostname === entry.domain.replace(/^\./, "") || hostname.endsWith(entry.domain)) {
          pairs.push(`${k}=${entry.value}`);
        }
      }
      return pairs.join("; ");
    },
    set cookie(raw) {
      const segs = String(raw).split(";").map((s) => s.trim());
      const [nv, ...attrs] = segs;
      const eq = nv.indexOf("=");
      if (eq < 0) return;
      const name = nv.slice(0, eq);
      const value = nv.slice(eq + 1);
      let domain = "";
      let maxAge = 3600;
      for (const a of attrs) {
        const [ak, av] = a.split("=");
        if (ak.toLowerCase() === "domain") domain = (av || "").toLowerCase();
        if (ak.toLowerCase() === "max-age") maxAge = Number(av);
      }
      // Reject public suffix `.tech` just like real browsers do
      if (domain === ".tech") return;
      if (maxAge <= 0) {
        jar.delete(name);
      } else {
        jar.set(name, { value, domain, maxAge });
      }
    },
  });

  const savedLoc = globalThis.location;
  try {
    globalThis.fetchLater = () => ({ activated: false });

    // Visit 1 on https://example.com (Origin A localStorage)
    globalThis.localStorage = makeMemoryStorage();
    globalThis.sessionStorage = makeMemoryStorage();
    Object.defineProperty(globalThis, "location", {
      value: { hostname: "example.com", protocol: "https:", pathname: "/", search: "", href: "https://example.com/" },
      configurable: true,
      writable: true,
    });
    Object.defineProperty(globalThis, "document", {
      value: makeDocWithCookieJar("example.com"),
      configurable: true,
      writable: true,
    });
    const apexClient = createLumaEye({
      endpoint: "https://eye.example.com",
      key: "pk_itool",
      project: "itool-tech",
      autoPageviews: false,
      autoVitals: false,
      autoErrors: false,
    });
    const apexActor = apexClient.getActorId();
    const apexSession = apexClient.getSessionId();

    // Visit 2 on https://www.example.com (Origin B isolated empty localStorage, shared root cookie!)
    globalThis.localStorage = makeMemoryStorage();
    globalThis.sessionStorage = makeMemoryStorage();
    Object.defineProperty(globalThis, "location", {
      value: { hostname: "www.example.com", protocol: "https:", pathname: "/other", search: "", href: "https://www.example.com/other" },
      configurable: true,
      writable: true,
    });
    Object.defineProperty(globalThis, "document", {
      value: makeDocWithCookieJar("www.example.com"),
      configurable: true,
      writable: true,
    });
    const wwwClient = createLumaEye({
      endpoint: "https://eye.example.com",
      key: "pk_itool",
      project: "itool-tech",
      autoPageviews: false,
      autoVitals: false,
      autoErrors: false,
    });

    assert.equal(wwwClient.getActorId(), apexActor);
    assert.equal(wwwClient.getSessionId(), apexSession);

    apexClient.destroy();
    wwwClient.destroy();
  } finally {
    if (savedLoc === undefined) {
      delete globalThis.location;
    } else {
      Object.defineProperty(globalThis, "location", { value: savedLoc, configurable: true, writable: true });
    }
  }
});
