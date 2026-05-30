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
      manager: isZh ? "等待初始化" : "waiting",
      worker: isZh ? "未加入" : "offline",
      client: isZh ? "CLI 已安装" : "CLI installed",
      domain: "luma.example.com",
      domainStatus: isZh ? "准备作为控制面入口" : "reserved for the control API",
      dns: isZh ? "等待中" : "pending",
      dnsStatus: isZh ? "安装阶段还不会修改 DNS" : "install does not touch DNS",
      tls: isZh ? "等待中" : "pending",
      tlsStatus: isZh ? "等 manager 创建路由后签发" : "issued after the manager creates routes",
      stack: isZh ? "未更新" : "not touched",
      stackStatus: isZh ? "Portainer 还未部署" : "Portainer is not deployed yet",
    },
    bootstrap: {
      manager: isZh ? "控制面在线" : "control online",
      worker: isZh ? "等待加入" : "waiting to join",
      client: isZh ? "保存 owner token" : "owner token saved",
      domain: "luma.example.com",
      domainStatus: isZh ? "控制面 HTTPS 入口已创建" : "control API HTTPS route created",
      dns: "luma.example.com -> manager",
      dnsStatus: isZh ? "A 记录指向 manager 公网入口" : "A record points to the manager edge",
      tls: isZh ? "证书已签发" : "certificate issued",
      tlsStatus: isZh ? "Traefik ACME 完成挑战" : "Traefik ACME challenge completed",
      stack: isZh ? "control + Portainer" : "control + Portainer",
      stackStatus: isZh ? "控制面组件以 Swarm service 运行" : "control plane runs as Swarm services",
    },
    join: {
      manager: isZh ? "发放 join token" : "join token issued",
      worker: isZh ? "global-worker 已加入" : "global-worker joined",
      client: isZh ? "可在任意机器 login" : "login from any machine",
      domain: "luma.example.com",
      domainStatus: isZh ? "worker 通过控制面注册" : "worker registers through the control API",
      dns: isZh ? "控制面记录保持" : "control record kept",
      dnsStatus: isZh ? "节点 labels 更新，不需要改服务域名" : "node labels change without service DNS changes",
      tls: isZh ? "继续有效" : "still valid",
      tlsStatus: isZh ? "worker 加入不影响已有证书" : "worker joins do not affect existing certs",
      stack: isZh ? "节点 labels 已写入" : "node labels applied",
      stackStatus: isZh ? "Portainer 可看到新节点和 region" : "Portainer can inspect the new node and region",
    },
    deploy: {
      manager: isZh ? "编排部署" : "orchestrating",
      worker: isZh ? "运行副本" : "running replicas",
      client: isZh ? "提交 status.yaml" : "submitted status.yaml",
      domain: "status.example.com",
      domainStatus: isZh ? "服务获得独立公网域名" : "service receives its own public domain",
      dns: "status.example.com -> cn-edge",
      dnsStatus: isZh ? "Cloudflare 指向选中的 edge 节点" : "Cloudflare points to the selected edge node",
      tls: isZh ? "证书已签发" : "certificate issued",
      tlsStatus: isZh ? "HTTPS 路由随 stack 一起生效" : "HTTPS route becomes active with the stack",
      stack: isZh ? "status stack 已更新" : "status stack updated",
      stackStatus: isZh ? "Portainer 展示服务、日志和副本位置" : "Portainer shows service, logs, and placement",
    },
  };

  const fields = {
    manager: demoStage.querySelector("[data-demo-manager]"),
    worker: demoStage.querySelector("[data-demo-worker]"),
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
