const TOKEN_KEY = "luma.dashboard.deployToken";
const LANG_KEY = "luma.dashboard.lang";
const I18N = {
  zh: {
    title: "发布控制台",
    subtitle: "查看控制面状态、服务器、服务和访问路径。",
    refresh: "刷新",
    signOut: "退出",
    readonly: "只读访问",
    loginTitle: "粘贴 deploy token",
    loginCopy: "Token 只保存在当前浏览器，并只发送给当前 Luma Control 域名。",
    openStatus: "打开面板",
    cluster: "集群",
    nodesEyebrow: "服务器",
    nodes: "节点",
    servicesEyebrow: "工作负载",
    services: "服务",
    pathsEyebrow: "拓扑",
    trafficPaths: "流量路径",
    inferred: "自动生成",
    name: "名称",
    region: "区域",
    role: "角色",
    state: "状态",
    availability: "可用性",
    leader: "Leader",
    service: "服务",
    exposure: "入口",
    image: "镜像",
    replicas: "副本",
    health: "健康",
    notConnected: "未连接",
    refreshing: "刷新中...",
    unavailable: "不可用",
    tokenRejected: "Token 无效",
    updated: "已更新",
    ready: "就绪",
    missing: "缺失",
    configured: "已配置",
    dockerReachable: "Docker socket 可访问",
    dockerUnavailable: "Docker socket 不可用",
    noPublicDomain: "无公开域名",
    running: "运行",
    pending: "等待",
    failed: "失败",
    yes: "是",
  },
  en: {
    title: "Deploy Console",
    subtitle: "Inspect control readiness, servers, services, and traffic paths.",
    refresh: "Refresh",
    signOut: "Sign out",
    readonly: "Read-only access",
    loginTitle: "Paste a deploy token",
    loginCopy: "The token stays in this browser and is only sent to this Luma Control origin.",
    openStatus: "Open status",
    cluster: "Cluster",
    nodesEyebrow: "Servers",
    nodes: "Nodes",
    servicesEyebrow: "Workloads",
    services: "Services",
    pathsEyebrow: "Topology",
    trafficPaths: "Traffic Paths",
    inferred: "Generated",
    name: "Name",
    region: "Region",
    role: "Role",
    state: "State",
    availability: "Availability",
    leader: "Leader",
    service: "Service",
    exposure: "Exposure",
    image: "Image",
    replicas: "Replicas",
    health: "Health",
    notConnected: "Not connected",
    refreshing: "Refreshing...",
    unavailable: "Unavailable",
    tokenRejected: "Token rejected",
    updated: "Updated",
    ready: "ready",
    missing: "missing",
    configured: "configured",
    dockerReachable: "Docker socket reachable",
    dockerUnavailable: "Docker socket unavailable",
    noPublicDomain: "No public domain",
    running: "run",
    pending: "pending",
    failed: "failed",
    yes: "yes",
  },
};

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  lang: localStorage.getItem(LANG_KEY) || "zh",
  timer: null,
  payload: null,
};

const $ = (selector) => document.querySelector(selector);
const nodesBody = $("[data-nodes]");
const servicesBody = $("[data-services]");
const pathsGrid = $("[data-paths]");
const loginPanel = $("[data-login-panel]");
const summary = $("[data-summary]");
const content = $("[data-content]");
const pathSection = $("[data-path-section]");
const errorsPanel = $("[data-errors]");
const syncState = $("[data-sync-state]");

document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
applyLocale();

$("[data-login-form]").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("[data-token-input]").value.trim();
  if (!value) return;
  state.token = value;
  localStorage.setItem(TOKEN_KEY, value);
  loadDashboard();
});

$("[data-refresh]").addEventListener("click", () => loadDashboard());
$("[data-sign-out]").addEventListener("click", () => {
  state.token = "";
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
});

document.querySelectorAll("[data-lang]").forEach((button) => {
  button.addEventListener("click", () => {
    state.lang = button.dataset.lang || "zh";
    localStorage.setItem(LANG_KEY, state.lang);
    document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
    applyLocale();
    if (state.payload) renderDashboard(state.payload);
    else showLogin();
  });
});

if (state.token) {
  loadDashboard();
} else {
  showLogin();
}

async function loadDashboard() {
  if (!state.token) {
    showLogin();
    return;
  }
  syncState.textContent = t("refreshing");
  try {
    const response = await fetch("/v1/dashboard", {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.payload = payload;
    renderDashboard(payload);
    syncState.textContent = `${t("updated")} ${new Date().toLocaleTimeString()}`;
    scheduleRefresh();
  } catch (error) {
    syncState.textContent = t("unavailable");
    showErrors([String(error.message || error)]);
    if (/unauthorized|bearer token/i.test(String(error.message || error))) showLogin(true);
  }
}

function scheduleRefresh() {
  window.clearTimeout(state.timer);
  state.timer = window.setTimeout(loadDashboard, 30000);
}

function showLogin(clearData = true) {
  loginPanel.hidden = false;
  summary.hidden = clearData;
  content.hidden = clearData;
  pathSection.hidden = clearData;
  if (clearData) errorsPanel.hidden = true;
  syncState.textContent = state.token ? t("tokenRejected") : t("notConnected");
}

function renderDashboard(payload) {
  loginPanel.hidden = true;
  summary.hidden = false;
  content.hidden = false;
  pathSection.hidden = false;
  renderSummary(payload);
  renderNodes(payload.nodes || []);
  renderServices(payload.services || []);
  renderPaths(payload.trafficPaths || []);
  showErrors(payload.errors || []);
}

function renderSummary(payload) {
  const cluster = payload.cluster || {};
  const readiness = payload.readiness || {};
  const dns = readiness.dns || {};
  const portainer = readiness.portainer || {};
  const swarm = readiness.swarm || {};
  $("[data-cluster-id]").textContent = cluster.id || "-";
  $("[data-cluster-version]").textContent = cluster.version ? `version ${cluster.version}` : "-";
  setReady("[data-dns-ready]", dns.ready);
  $("[data-dns-detail]").textContent = [dns.provider, dns.zone, dns.target].filter(Boolean).join(" / ") || "-";
  setReady("[data-portainer-ready]", portainer.ready);
  $("[data-portainer-detail]").textContent = `api ${flag(portainer.apiConfigured)}, endpoint ${flag(portainer.endpointConfigured)}`;
  setReady("[data-swarm-ready]", swarm.available);
  $("[data-swarm-detail]").textContent = swarm.available ? t("dockerReachable") : t("dockerUnavailable");
}

function renderNodes(nodes) {
  $("[data-node-count]").textContent = String(nodes.length);
  nodesBody.replaceChildren(...nodes.map((node) => row([
    primaryCell(node.name, node.displayName),
    badge(node.region || "-"),
    node.role || "-",
    statePill(node.state),
    node.availability || "-",
    node.leader ? t("yes") : "-",
  ])));
}

function renderServices(services) {
  $("[data-service-count]").textContent = String(services.length);
  servicesBody.replaceChildren(...services.map((service) => row([
    primaryCell(service.stack ? `${service.stack}/${service.name}` : service.name, service.fullName),
    badge(service.region || "-"),
    badge(service.exposure || "none"),
    codeCell(service.image || "-"),
    `${service.running}/${service.desired} ${t("running")}, ${service.pending} ${t("pending")}, ${service.failed} ${t("failed")}`,
    statePill(service.health),
    (service.nodes || []).join(", ") || "-",
  ])));
}

function renderPaths(paths) {
  pathsGrid.replaceChildren(...paths.map((path) => {
    const card = document.createElement("article");
    card.className = "path-card";
    const title = document.createElement("div");
    title.className = "path-title";
    title.append(primaryCell(path.id || "-", path.domain || t("noPublicDomain")), badge(path.kind || "unknown"));
    const flow = document.createElement("div");
    flow.className = "path-flow";
    (path.segments || []).forEach((segment, index) => {
      flow.append(pill(segment));
      if (index < path.segments.length - 1) flow.append(arrow());
    });
    card.append(title, flow);
    return card;
  }));
}

function showErrors(errors) {
  errorsPanel.replaceChildren(...errors.map((message) => {
    const item = document.createElement("div");
    item.textContent = message;
    return item;
  }));
  errorsPanel.hidden = errors.length === 0;
}

function row(cells) {
  const tr = document.createElement("tr");
  cells.forEach((cell) => {
    const td = document.createElement("td");
    if (cell instanceof Node) td.append(cell);
    else td.textContent = String(cell);
    tr.append(td);
  });
  return tr;
}

function primaryCell(title, meta) {
  const wrap = document.createElement("span");
  wrap.className = "primary-cell";
  wrap.append(strong(title || "-"));
  if (meta && meta !== title) {
    const small = document.createElement("small");
    small.textContent = meta;
    wrap.append(small);
  }
  return wrap;
}

function codeCell(value) {
  const code = document.createElement("code");
  code.textContent = value;
  return code;
}

function statePill(value) {
  const span = badge(localizeState(value || "-"));
  span.classList.add(["ready", "running", "healthy"].includes(value) ? "good" : ["failed", "missing", "bad"].includes(value) ? "danger" : "warn");
  return span;
}

function badge(value) {
  const span = document.createElement("span");
  span.className = "badge";
  span.textContent = value;
  return span;
}

function pill(value) {
  const span = document.createElement("span");
  span.className = "path-pill";
  span.textContent = value;
  return span;
}

function arrow() {
  const span = document.createElement("span");
  span.className = "path-arrow";
  span.textContent = ">";
  return span;
}

function strong(value) {
  const item = document.createElement("strong");
  item.textContent = value;
  return item;
}

function setReady(selector, value) {
  const element = $(selector);
  element.textContent = value ? t("ready") : t("missing");
  element.className = value ? "ok" : "bad";
}

function localizeState(value) {
  const zh = {
    ready: "就绪",
    healthy: "健康",
    running: "运行",
    pending: "等待",
    failed: "失败",
    degraded: "降级",
    missing: "缺失",
    unknown: "未知",
  };
  return state.lang === "zh" ? (zh[value] || value) : value;
}

function flag(value) {
  return value ? t("configured") : t("missing");
}

function applyLocale() {
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-lang]").forEach((button) => {
    button.classList.toggle("active", button.dataset.lang === state.lang);
  });
  syncState.textContent = state.token ? syncState.textContent : t("notConnected");
}

function t(key) {
  return (I18N[state.lang] || I18N.zh)[key] || key;
}
