const TOKEN_KEY = "luma.dashboard.deployToken";
const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  timer: null,
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
  syncState.textContent = "Refreshing...";
  try {
    const response = await fetch("/v1/dashboard", {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderDashboard(payload);
    syncState.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    scheduleRefresh();
  } catch (error) {
    syncState.textContent = "Unavailable";
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
  syncState.textContent = state.token ? "Token rejected" : "Not connected";
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
  $("[data-dns-ready]").textContent = yesNo(dns.ready);
  $("[data-dns-ready]").className = dns.ready ? "ok" : "bad";
  $("[data-dns-detail]").textContent = [dns.provider, dns.zone, dns.target].filter(Boolean).join(" / ") || "-";
  $("[data-portainer-ready]").textContent = yesNo(portainer.ready);
  $("[data-portainer-ready]").className = portainer.ready ? "ok" : "bad";
  $("[data-portainer-detail]").textContent = `api ${flag(portainer.apiConfigured)}, endpoint ${flag(portainer.endpointConfigured)}`;
  $("[data-swarm-ready]").textContent = yesNo(swarm.available);
  $("[data-swarm-ready]").className = swarm.available ? "ok" : "bad";
  $("[data-swarm-detail]").textContent = swarm.available ? "Docker socket reachable" : "Docker socket unavailable";
}

function renderNodes(nodes) {
  $("[data-node-count]").textContent = String(nodes.length);
  nodesBody.replaceChildren(...nodes.map((node) => row([
    primaryCell(node.name, node.displayName),
    badge(node.region || "-"),
    node.role || "-",
    statePill(node.state),
    node.availability || "-",
    node.leader ? "yes" : "-",
  ])));
}

function renderServices(services) {
  $("[data-service-count]").textContent = String(services.length);
  servicesBody.replaceChildren(...services.map((service) => row([
    primaryCell(service.stack ? `${service.stack}/${service.name}` : service.name, service.fullName),
    badge(service.region || "-"),
    badge(service.exposure || "none"),
    codeCell(service.image || "-"),
    `${service.running}/${service.desired} run, ${service.pending} pending, ${service.failed} failed`,
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
    title.append(strong(path.id || "-"), badge(path.kind || "unknown"));
    const domain = document.createElement("p");
    domain.textContent = path.domain || "No public domain";
    const flow = document.createElement("div");
    flow.className = "path-flow";
    (path.segments || []).forEach((segment, index) => {
      flow.append(pill(segment));
      if (index < path.segments.length - 1) flow.append(arrow());
    });
    card.append(title, domain, flow);
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
  const span = badge(value || "-");
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

function yesNo(value) {
  return value ? "ready" : "missing";
}

function flag(value) {
  return value ? "configured" : "missing";
}
