import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import {
  attachNavigationContainer,
  createAsyncStorage,
  createLumaEyeRN,
  createMMKVStorage,
  createPathnameTracker,
} from "../dist/react-native.mjs";

function makeFakeMMKV() {
  const store = new Map();
  return {
    store,
    getString: (k) => (store.has(k) ? store.get(k) : undefined),
    set: (k, v) => store.set(k, String(v)),
    delete: (k) => store.delete(k),
  };
}

function makeFakeAsyncStorage() {
  const store = new Map();
  return {
    store,
    async getItem(k) {
      return store.has(k) ? store.get(k) : null;
    },
    async setItem(k, v) {
      store.set(k, String(v));
    },
    async removeItem(k) {
      store.delete(k);
    },
  };
}

function makeFakeAppState(initial = "active") {
  const listeners = new Set();
  return {
    currentState: initial,
    addEventListener(_type, fn) {
      listeners.add(fn);
      return { remove: () => listeners.delete(fn) };
    },
    emit(nextState) {
      this.currentState = nextState;
      for (const fn of listeners) fn(nextState);
    },
  };
}

function makeFakeErrorUtils() {
  const previousCalls = [];
  const origHandler = (err, isFatal) => {
    previousCalls.push({ err, isFatal });
  };
  let handler = origHandler;
  return {
    previousCalls,
    origHandler,
    getGlobalHandler: () => handler,
    setGlobalHandler: (fn) => {
      handler = fn;
    },
    trigger: (err, isFatal) => handler(err, isFatal),
  };
}

let savedFetch;

beforeEach(() => {
  savedFetch = globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = savedFetch;
});

test("React Native ErrorUtils captures fatal crashes with Ring Buffer breadcrumbs and chains previous handler", async () => {
  const requests = [];
  const apiCalls = [];
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith("https://eye.example.com")) {
      requests.push({ url, body: JSON.parse(init.body) });
      return { status: 204 };
    }
    apiCalls.push({ url, headers: new Headers(init?.headers) });
    return { status: 200 };
  };

  const appState = makeFakeAppState("active");
  const errorUtils = makeFakeErrorUtils();
  const mmkv = makeFakeMMKV();

  const rn = createLumaEyeRN({
    endpoint: "https://eye.example.com",
    key: "pk_rn_test",
    project: "rnshop",
    env: "production",
    release: "1.4.0+42",
    storage: createMMKVStorage(mmkv),
    appState,
    platform: { OS: "ios", isPad: false },
    errorUtils,
    traceOrigins: ["https://api.example.com"],
  });

  // Ring Buffer keeps debug logs silently without network calls
  rn.addBreadcrumb("ui.tap", "Pressable#CheckoutBtn");
  rn.log("debug", "cart validation token=secret_xyz");
  await globalThis.fetch("https://api.example.com/v1/checkout", { method: "POST" });

  // Checkout call received W3C traceparent; telemetry endpoint has 0 requests so far
  assert.equal(apiCalls.length, 1);
  assert.match(apiCalls[0].headers.get("traceparent") || "", /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  assert.equal(requests.length, 0);

  // Trigger native JS fatal crash via ErrorUtils
  const fatalErr = new TypeError("Cannot read property 'total' of null");
  errorUtils.trigger(fatalErr, true);
  await new Promise((r) => setTimeout(r, 15));

  assert.equal(errorUtils.previousCalls.length, 1);
  assert.equal(errorUtils.previousCalls[0].isFatal, true);
  assert.equal(requests.length, 1);

  const envelope = requests[0].body;
  assert.equal(envelope.track, "urgent");
  assert.equal(envelope.sdk, "luma-eye-rn/1.1.0");
  assert.equal(envelope.os, "iOS");
  assert.equal(envelope.device_type, "mobile");
  assert.equal(envelope.errors[0].level, "fatal");
  assert.equal(envelope.errors[0].tags.source, "ErrorUtils");
  assert.equal(envelope.errors[0].tags.fatal, "true");
  assert.ok(envelope.errors[0].breadcrumbs.some((b) => b.message === "Pressable#CheckoutBtn"));
  assert.ok(envelope.errors[0].breadcrumbs.some((b) => b.category === "http" && b.data?.trace_id));

  rn.destroy();
  // Original ErrorUtils handler restored cleanly on destroy
  assert.equal(errorUtils.getGlobalHandler(), errorUtils.origHandler);
});

test("React Native AppState background flush pauses foreground clock and persists MMKV actor_id + offline outbox", async () => {
  const deliveries = [];
  let offline = true;
  globalThis.fetch = async (url, init) => {
    if (offline) {
      throw new Error("Network request failed");
    }
    deliveries.push({ url, body: JSON.parse(init.body) });
    return { status: 204 };
  };

  const mmkv = makeFakeMMKV();
  const appState1 = makeFakeAppState("active");
  const errorUtils = makeFakeErrorUtils();

  const rn1 = createLumaEyeRN({
    endpoint: "https://eye.example.com",
    key: "pk_rn_offline",
    project: "rnoffline",
    storage: createMMKVStorage(mmkv),
    appState: appState1,
    platform: { OS: "android", isPad: false },
    errorUtils,
    batchWindowMs: 5000,
    autoNetworkBreadcrumbs: false,
  });

  rn1.screen("HomeScreen");
  rn1.track("add_to_cart", { sku: "pro-plan" });
  const actor1 = mmkv.getString("__luma_eye_uid_rnoffline__");
  assert.ok(actor1 && actor1.startsWith("u_"));

  // Moving to background immediately flushes Track 1, pauses foreground clock, and saves to MMKV outbox
  appState1.emit("background");
  const fgAtBg = rn1.getForegroundTimeMs();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(rn1.getForegroundTimeMs(), fgAtBg);
  assert.equal(deliveries.length, 0);
  const outboxRaw = mmkv.getString("__luma_eye_outbox_rnoffline__");
  assert.ok(outboxRaw && outboxRaw.includes("add_to_cart"));
  rn1.destroy();

  // Next launch online with same MMKV storage keeps same actor_id and drains persisted batch!
  offline = false;
  const appState2 = makeFakeAppState("active");
  const rn2 = createLumaEyeRN({
    endpoint: "https://eye.example.com",
    key: "pk_rn_offline",
    project: "rnoffline",
    storage: createMMKVStorage(mmkv),
    appState: appState2,
    platform: { OS: "android", isPad: false },
    errorUtils,
    autoNetworkBreadcrumbs: false,
  });

  await new Promise((r) => setTimeout(r, 20));
  assert.ok(deliveries.length >= 1);
  const recovered = deliveries.find((d) => d.body.events?.some((e) => e.name === "add_to_cart"));
  assert.ok(recovered, "expected offline normal batch to be drained on next boot");
  assert.equal(recovered.body.actor_id, actor1);
  assert.equal(recovered.body.os, "Android");
  assert.equal(mmkv.getString("__luma_eye_outbox_rnoffline__"), undefined);
  rn2.destroy();
});

test("React Native AsyncStorage adapter hydrates actor_id and drains offline crash outbox when returning active", async () => {
  const sent = [];
  let offline = true;
  globalThis.fetch = async (url, init) => {
    if (offline) throw new Error("airplane mode");
    sent.push({ url, body: JSON.parse(init.body) });
    return { status: 204 };
  };

  const asyncStore = makeFakeAsyncStorage();
  await asyncStore.setItem("__luma_eye_uid_rnasync__", "user_async_42");
  const appState = makeFakeAppState("active");
  const errorUtils = makeFakeErrorUtils();

  const rn = createLumaEyeRN({
    endpoint: "https://eye.example.com",
    key: "pk_rn_async",
    project: "rnasync",
    storage: createAsyncStorage(asyncStore),
    appState,
    platform: { OS: "ios", isPad: true },
    errorUtils,
    autoNetworkBreadcrumbs: false,
  });

  // Let AsyncStorage promise hydrate actorId
  await new Promise((r) => setTimeout(r, 15));

  rn.captureError(new Error("offline boom"));
  await new Promise((r) => setTimeout(r, 15));
  assert.equal(sent.length, 0);
  const rawOutbox = await asyncStore.getItem("__luma_eye_outbox_rnasync__");
  assert.ok(rawOutbox && rawOutbox.includes("offline boom"));

  // Come back online and bring app to foreground -> drains outbox automatically
  offline = false;
  appState.emit("background");
  appState.emit("active");
  await new Promise((r) => setTimeout(r, 25));

  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.actor_id, "user_async_42");
  assert.equal(sent[0].body.device_type, "tablet");
  assert.equal(await asyncStore.getItem("__luma_eye_outbox_rnasync__"), null);
  rn.destroy();
});

test("attachNavigationContainer and createPathnameTracker record screen pageviews and deduplicate transitions", async () => {
  const sent = [];
  globalThis.fetch = async (_url, init) => {
    sent.push(JSON.parse(init.body));
    return { status: 204 };
  };

  const appState = makeFakeAppState("active");
  const rn = createLumaEyeRN({
    endpoint: "https://eye.example.com",
    key: "pk_nav",
    project: "rnnav",
    appState,
    platform: { OS: "ios" },
    errorUtils: makeFakeErrorUtils(),
    autoNetworkBreadcrumbs: false,
  });

  let currentRoute = { name: "FeedScreen", key: "k1" };
  let stateListener = null;
  const fakeNavRef = {
    getCurrentRoute: () => currentRoute,
    addListener: (ev, fn) => {
      assert.equal(ev, "state");
      stateListener = fn;
      return () => {
        stateListener = null;
      };
    },
  };

  const nav = attachNavigationContainer(fakeNavRef, { client: rn });
  nav.onReady();
  // Calling onStateChange without route change deduplicates
  nav.onStateChange();

  currentRoute = { name: "CheckoutScreen", key: "k2" };
  stateListener();

  const trackPathname = createPathnameTracker(rn);
  trackPathname("/settings");
  trackPathname("/settings"); // deduplicated

  rn.flush();
  await new Promise((r) => setTimeout(r, 15));

  assert.equal(sent.length, 1);
  const paths = sent[0].events.map((e) => e.page_path);
  assert.deepEqual(paths, ["/FeedScreen", "/CheckoutScreen", "/settings"]);
  const crumbs = rn.getRingBufferSnapshot().filter((b) => b.category === "navigation");
  assert.ok(crumbs.some((c) => c.message.includes("FeedScreen -> CheckoutScreen")));

  nav.detach();
  rn.destroy();
});

test("React Native Session keeps session_id across short backgrounding and rotates after 30m idle", () => {
  let fakeNow = 1_700_000_000_000;
  const origNow = Date.now;
  Date.now = () => fakeNow;
  globalThis.fetch = async () => ({ status: 204 });

  try {
    const mmkv = makeFakeMMKV();
    const appState = makeFakeAppState("active");
    const rn = createLumaEyeRN({
      endpoint: "https://eye.example.com",
      key: "pk_rn_sess",
      project: "rnsess",
      storage: createMMKVStorage(mmkv),
      appState,
      platform: { OS: "ios" },
      errorUtils: makeFakeErrorUtils(),
      autoNetworkBreadcrumbs: false,
    });

    const sid1 = rn.getSessionId();
    // Background for 10 minutes -> returning active keeps same session
    appState.emit("background");
    fakeNow += 10 * 60 * 1000;
    appState.emit("active");
    assert.equal(rn.getSessionId(), sid1);

    // Background for 35 minutes -> returning active rotates to new session
    appState.emit("background");
    fakeNow += 35 * 60 * 1000;
    appState.emit("active");
    const sid2 = rn.getSessionId();
    assert.notEqual(sid2, sid1);

    rn.destroy();
  } finally {
    Date.now = origNow;
  }
});
