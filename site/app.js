const revealItems = document.querySelectorAll(".reveal");

const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
        observer.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.16 }
);

revealItems.forEach((item, index) => {
  item.style.transitionDelay = `${Math.min(index * 45, 220)}ms`;
  observer.observe(item);
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const value = button.getAttribute("data-copy") || "";
    try {
      await navigator.clipboard.writeText(value);
      const previous = button.textContent;
      button.textContent = document.documentElement.lang === "zh-CN" ? "已复制" : "Copied";
      window.setTimeout(() => {
        button.textContent = previous;
      }, 1200);
    } catch {
      button.textContent = document.documentElement.lang === "zh-CN" ? "手动选择" : "Select";
    }
  });
});

const demoStage = document.querySelector(".demo-stage");
const demoButtons = Array.from(document.querySelectorAll("[data-demo-step]"));

if (demoStage && demoButtons.length) {
  const isZh = document.documentElement.lang === "zh-CN";
  const demoCopy = {
    install: {
      manager: isZh ? "等待初始化" : "waiting to bootstrap",
      edge: isZh ? "未创建" : "not created",
      worker: isZh ? "未加入" : "not joined",
      global: isZh ? "未加入" : "not joined",
      client: isZh ? "CLI 已安装" : "CLI installed",
      domain: isZh ? "luma 命令就绪" : "luma command ready",
      domainStatus: isZh ? "manager、worker、client 使用同一套 CLI" : "managers, workers, and clients use the same CLI",
      dns: isZh ? "未配置" : "not configured",
      dnsStatus: isZh ? "安装阶段只准备本机命令" : "install only prepares the local command",
      tls: isZh ? "未创建" : "not created",
      tlsStatus: isZh ? "证书会随控制面和服务域名创建" : "certificates are created with control and service hosts",
      stack: isZh ? "无工作负载" : "no workload",
      stackStatus: isZh ? "集群尚未初始化" : "the cluster is not initialized yet",
    },
    bootstrap: {
      manager: isZh ? "Swarm manager + Control API" : "Swarm manager + Control API",
      edge: isZh ? "Traefik 已启动" : "Traefik running",
      worker: isZh ? "等待加入" : "waiting to join",
      global: isZh ? "等待加入" : "waiting to join",
      client: isZh ? "拿到 deploy token" : "deploy token ready",
      domain: "luma.example.com",
      domainStatus: isZh ? "控制面域名变成登录入口" : "control domain becomes the login endpoint",
      dns: "luma.example.com -> manager",
      dnsStatus: isZh ? "登录入口先对外可达" : "the login endpoint becomes reachable first",
      tls: isZh ? "控制面证书" : "control certificate",
      tlsStatus: isZh ? "Traefik 为控制面建立 HTTPS" : "Traefik creates HTTPS for control",
      stack: isZh ? "控制面服务运行" : "control services running",
      stackStatus: isZh ? "后续部署提交到这个 endpoint" : "later deploys are submitted to this endpoint",
    },
    "join-cn": {
      manager: isZh ? "写入节点 labels" : "node labels applied",
      edge: isZh ? "cn-edge 可承接入口" : "cn-edge can receive traffic",
      worker: isZh ? "region=cn / profile=cn-edge" : "region=cn / profile=cn-edge",
      global: isZh ? "等待加入" : "waiting to join",
      client: isZh ? "控制面记录节点" : "control records the node",
      domain: isZh ? "国内服务可选 cn-edge" : "CN services can target cn-edge",
      domainStatus: isZh ? "manifest 通过 exposure 选择入口节点" : "manifest chooses the entry node with exposure",
      dns: isZh ? "等待服务域名" : "waiting for service domain",
      dnsStatus: isZh ? "业务域名在 deploy 时写入" : "service DNS is written during deploy",
      tls: isZh ? "等待服务域名" : "waiting for service domain",
      tlsStatus: isZh ? "业务证书跟服务域名绑定" : "service certificates bind to service domains",
      stack: isZh ? "CN worker 可调度" : "CN worker schedulable",
      stackStatus: isZh ? "符合条件的服务可以调度到该节点" : "matching services can be placed on this node",
    },
    "join-global": {
      manager: isZh ? "多节点视图完成" : "multi-node view ready",
      edge: isZh ? "边缘路由在线" : "edge routing online",
      worker: isZh ? "cn-edge 在线" : "cn-edge online",
      global: isZh ? "region=global / profile=global-worker" : "region=global / profile=global-worker",
      client: isZh ? "任意机器可部署" : "any machine can deploy",
      domain: isZh ? "可按服务选择区域" : "services can choose region",
      domainStatus: isZh ? "例如 status 走 cn-edge，api 走 global-worker" : "for example, status uses cn-edge and api uses global-worker",
      dns: isZh ? "按 exposure 写记录" : "records follow exposure",
      dnsStatus: isZh ? "不同域名可以指向不同入口" : "different domains can point at different entries",
      tls: isZh ? "按域名签发" : "issued per domain",
      tlsStatus: isZh ? "每个服务域名独立获得 HTTPS" : "each service host receives HTTPS",
      stack: isZh ? "多节点可调度" : "multi-node scheduling",
      stackStatus: isZh ? "控制面按标签选择合适节点" : "control selects suitable nodes by labels",
    },
    deploy: {
      manager: isZh ? "接收 status.yaml" : "received status.yaml",
      edge: isZh ? "创建 Host 路由" : "creating Host route",
      worker: "status:80 x2",
      global: isZh ? "保持可用" : "still available",
      client: isZh ? "提交服务清单" : "submitted service manifest",
      domain: "status.example.com",
      domainStatus: isZh ? "manifest 提供 domain 和 port" : "manifest provides domain and port",
      dns: "status.example.com -> cn-edge",
      dnsStatus: isZh ? "Cloudflare 记录指向 cn-edge" : "Cloudflare record points to cn-edge",
      tls: isZh ? "HTTPS 建立中" : "HTTPS being created",
      tlsStatus: isZh ? "Traefik 根据 Host 规则执行 ACME" : "Traefik runs ACME from the Host rule",
      stack: "Host(status.example.com) -> status:80",
      stackStatus: isZh ? "路由连接到 Swarm 服务副本" : "route attaches to Swarm service replicas",
    },
    published: {
      manager: isZh ? "部署记录已保存" : "deployment recorded",
      edge: isZh ? "HTTPS 入口在线" : "HTTPS entry online",
      worker: "status:80 x2",
      global: isZh ? "可部署下一个服务" : "ready for next service",
      client: isZh ? "得到访问地址" : "received public URL",
      domain: "https://status.example.com",
      domainStatus: isZh ? "访问域名即可进入服务" : "the domain now reaches the service",
      dns: "status.example.com -> cn-edge",
      dnsStatus: isZh ? "DNS、证书、路由都已生效" : "DNS, certificate, and route are active",
      tls: isZh ? "HTTPS 可用" : "HTTPS active",
      tlsStatus: isZh ? "证书生命周期交给 Traefik" : "certificate lifecycle stays with Traefik",
      stack: "status service healthy",
      stackStatus: isZh ? "工作负载在集群节点上运行" : "workload runs on cluster nodes",
    },
  };

  const fields = {
    manager: demoStage.querySelector("[data-demo-manager]"),
    edge: demoStage.querySelector("[data-demo-edge]"),
    worker: demoStage.querySelector("[data-demo-worker]"),
    global: demoStage.querySelector("[data-demo-global]"),
    client: demoStage.querySelector("[data-demo-client]"),
    domain: demoStage.querySelector("[data-demo-domain]"),
    domainStatus: demoStage.querySelector("[data-demo-domain-status]"),
    dns: demoStage.querySelector("[data-demo-dns]"),
    dnsStatus: demoStage.querySelector("[data-demo-dns-status]"),
    tls: demoStage.querySelector("[data-demo-tls]"),
    tlsStatus: demoStage.querySelector("[data-demo-tls-status]"),
    stack: demoStage.querySelector("[data-demo-stack]"),
    stackStatus: demoStage.querySelector("[data-demo-stack-status]"),
  };

  let activeIndex = 0;
  let timer;

  const activateStep = (step, shouldResetTimer = true) => {
    const copy = demoCopy[step];
    if (!copy) return;

    demoStage.dataset.stage = step;
    Object.entries(fields).forEach(([key, element]) => {
      if (element) element.textContent = copy[key];
    });

    demoButtons.forEach((button, index) => {
      const isActive = button.dataset.demoStep === step;
      button.classList.toggle("active", isActive);
      if (isActive) activeIndex = index;
    });

    if (shouldResetTimer) {
      window.clearInterval(timer);
      timer = window.setInterval(() => {
        activeIndex = (activeIndex + 1) % demoButtons.length;
        activateStep(demoButtons[activeIndex].dataset.demoStep, false);
      }, 3800);
    }
  };

  demoButtons.forEach((button) => {
    button.addEventListener("click", () => activateStep(button.dataset.demoStep));
  });

  activateStep(demoButtons[0].dataset.demoStep);
}
