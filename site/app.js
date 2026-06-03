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
    const label = button.querySelector("[data-command-copy-label]");
    try {
      await copyText(value);
      const copiedText = button.getAttribute("data-copied-label") || (document.documentElement.lang === "zh-CN" ? "已复制" : "Copied");
      if (label) {
        const previous = label.textContent;
        label.textContent = copiedText;
        button.classList.add("copied");
        window.setTimeout(() => {
          label.textContent = previous;
          button.classList.remove("copied");
        }, 1200);
        return;
      }
      const previous = button.textContent;
      button.textContent = copiedText;
      window.setTimeout(() => {
        button.textContent = previous;
      }, 1200);
    } catch {
      if (label) {
        label.textContent = document.documentElement.lang === "zh-CN" ? "手动选择" : "Select";
      } else {
        button.textContent = document.documentElement.lang === "zh-CN" ? "手动选择" : "Select";
      }
    }
  });
});

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall back for embedded or permission-restricted browsers.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, value.length);
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("copy failed");
}

const demoStage = document.querySelector(".demo-stage");
const demoButtons = Array.from(document.querySelectorAll("[data-demo-step]"));

if (demoStage && demoButtons.length) {
  const isZh = document.documentElement.lang === "zh-CN";
  const demoCopy = {
    install: {
      phase: isZh ? "Manager 安装 CLI" : "Install CLI on Manager",
      activity: isZh ? "在 manager 上安装 luma" : "Installing luma on the manager",
      manager: isZh ? "CLI 已安装" : "CLI installed",
      edge: isZh ? "未创建" : "not created",
      worker: isZh ? "未加入" : "not joined",
      global: isZh ? "未加入" : "not joined",
      client: isZh ? "未登录" : "not logged in",
      domain: isZh ? "manager 命令就绪" : "manager command ready",
      domainStatus: isZh ? "bootstrap 下一步在 manager 上执行" : "bootstrap runs on the manager next",
      dns: isZh ? "未配置" : "not configured",
      dnsStatus: isZh ? "这一步还不改 DNS" : "DNS is not changed here",
      tls: isZh ? "未创建" : "not created",
      tlsStatus: isZh ? "后面有域名时再申请" : "created later when hosts exist",
      stack: isZh ? "无工作负载" : "no workload",
      stackStatus: isZh ? "集群尚未初始化" : "the cluster is not initialized yet",
    },
    bootstrap: {
      phase: isZh ? "初始化 Manager" : "Bootstrap Manager",
      activity: isZh ? "在第一台服务器上建 Swarm" : "Initializes Swarm on the first server",
      manager: isZh ? "Swarm manager + Control API" : "Swarm manager + Control API",
      edge: isZh ? "Traefik 已启动" : "Traefik running",
      worker: isZh ? "等待加入" : "waiting to join",
      global: isZh ? "等待加入" : "waiting to join",
      client: isZh ? "保存部署 token" : "deploy token saved",
      domain: "luma.example.com",
      domainStatus: isZh ? "以后用这个地址登录" : "used later for login",
      dns: "luma.example.com -> manager",
      dnsStatus: isZh ? "先让控制 API 能访问" : "control API becomes reachable",
      tls: isZh ? "控制面证书" : "control certificate",
      tlsStatus: isZh ? "Traefik 给控制 API 申请证书" : "Traefik gets a certificate for control",
      stack: isZh ? "基础服务已启动" : "base services running",
      stackStatus: isZh ? "后续 deploy 提交到这里" : "later deploys go here",
    },
    "join-cn": {
      phase: isZh ? "加入 CN 节点" : "Join CN Node",
      activity: isZh ? "这台服务器在本机 join，并写入 region=cn" : "This node joins locally with region=cn",
      manager: isZh ? "写入节点 labels" : "node labels applied",
      edge: isZh ? "cn-edge 可用" : "cn-edge available",
      worker: isZh ? "region=cn / name=cn-worker-1" : "region=cn / name=cn-worker-1",
      global: isZh ? "等待加入" : "waiting to join",
      client: isZh ? "节点已记录" : "node recorded",
      domain: isZh ? "服务可选 cn-edge" : "services can use cn-edge",
      domainStatus: isZh ? "manifest 按 region 选择节点" : "manifest uses region for node placement",
      dns: isZh ? "等待服务域名" : "waiting for service domain",
      dnsStatus: isZh ? "deploy 时写入服务域名" : "service DNS is written during deploy",
      tls: isZh ? "等待服务域名" : "waiting for service domain",
      tlsStatus: isZh ? "有 Host 后再申请证书" : "certificate waits for a Host",
      stack: isZh ? "CN worker 可用" : "CN worker available",
      stackStatus: isZh ? "region=cn 的服务可放到这里" : "region=cn services can run here",
    },
    "join-global": {
      phase: isZh ? "加入 Global 节点" : "Join Global Node",
      activity: isZh ? "再加入一台 global 节点" : "Adds a global node",
      manager: isZh ? "已有多台节点" : "multiple nodes recorded",
      edge: isZh ? "边缘路由在线" : "edge routing online",
      worker: isZh ? "cn-edge 在线" : "cn-edge online",
      global: isZh ? "region=global / name=global-sg-1" : "region=global / name=global-sg-1",
      client: isZh ? "客户端可提交部署" : "client can deploy",
      domain: isZh ? "服务自己选 region" : "service chooses region",
      domainStatus: isZh ? "例如 status 用 cn，api 用 global" : "for example, status uses cn and api uses global",
      dns: isZh ? "按 exposure 写" : "written by exposure",
      dnsStatus: isZh ? "不同服务可使用不同入口" : "different services can use different entries",
      tls: isZh ? "按域名签发" : "issued per domain",
      tlsStatus: isZh ? "每个服务域名单独处理" : "handled per service host",
      stack: isZh ? "可按 region 放置" : "placed by region",
      stackStatus: isZh ? "按节点标签选择位置" : "placement uses node labels",
    },
    deploy: {
      phase: isZh ? "部署服务" : "Deploy Service",
      activity: isZh ? "读取 status.yaml，开始更新" : "Reads status.yaml and starts updating",
      manager: isZh ? "接收 status.yaml" : "received status.yaml",
      edge: isZh ? "写 Host 路由" : "writing Host route",
      worker: "status:80 x2",
      global: isZh ? "保持可用" : "still available",
      client: isZh ? "提交 manifest" : "manifest submitted",
      domain: "status.example.com",
      domainStatus: isZh ? "manifest 里有 domain 和 port" : "manifest has domain and port",
      dns: "status.example.com -> cn-edge",
      dnsStatus: isZh ? "Cloudflare DNS 记录指向 cn-edge" : "Cloudflare DNS record points to cn-edge",
      tls: isZh ? "申请 HTTPS" : "requesting HTTPS",
      tlsStatus: isZh ? "Traefik 按 Host 规则申请证书" : "Traefik requests cert from the Host rule",
      stack: "Host(status.example.com) -> status:80",
      stackStatus: isZh ? "路由指向 Swarm 服务" : "route points to the Swarm service",
    },
    published: {
      phase: isZh ? "发布完成" : "Published",
      activity: isZh ? "域名已经能访问服务" : "The domain now reaches the service",
      manager: isZh ? "部署记录已保存" : "deployment record saved",
      edge: isZh ? "HTTPS 可访问" : "HTTPS reachable",
      worker: "status:80 x2",
      global: isZh ? "空闲" : "idle",
      client: isZh ? "返回 URL" : "URL returned",
      domain: "https://status.example.com",
      domainStatus: isZh ? "打开域名即可访问服务" : "opening the domain reaches the service",
      dns: "status.example.com -> cn-edge",
      dnsStatus: isZh ? "DNS、证书、路由已更新" : "DNS, certificate, and route updated",
      tls: isZh ? "HTTPS 可用" : "HTTPS active",
      tlsStatus: isZh ? "证书后续由 Traefik 续期" : "Traefik renews the certificate later",
      stack: "status service healthy",
      stackStatus: isZh ? "服务在节点上运行" : "service is running on nodes",
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
    phase: document.querySelector("[data-demo-phase]"),
    progress: document.querySelector("[data-demo-progress]"),
    progressBar: document.querySelector("[data-demo-progress-bar]"),
    activity: demoStage.querySelector("[data-demo-activity]"),
  };

  let activeIndex = 0;
  let timer;

  const activateStep = (step, shouldResetTimer = true) => {
    const copy = demoCopy[step];
    if (!copy) return;

    demoStage.dataset.stage = step;
    Object.entries(fields).forEach(([key, element]) => {
      if (element && Object.prototype.hasOwnProperty.call(copy, key)) element.textContent = copy[key];
    });

    demoButtons.forEach((button, index) => {
      const isActive = button.dataset.demoStep === step;
      button.classList.toggle("active", isActive);
      if (isActive) {
        activeIndex = index;
        fields.progress?.style.setProperty("--progress", `${((index + 1) / demoButtons.length) * 100}%`);
        if (fields.progress) fields.progress.textContent = `${String(index + 1).padStart(2, "0")} / ${String(demoButtons.length).padStart(2, "0")}`;
        fields.progressBar?.style.setProperty("--progress", `${((index + 1) / demoButtons.length) * 100}%`);
      }
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

// Markdown Parser and Docs Modal Overlay Handler
(function initDocsModal() {
  const modal = document.getElementById("docsModal");
  const modalTitle = document.getElementById("docsModalTitle");
  const modalBody = document.getElementById("docsModalBody");
  const closeBtn = document.getElementById("closeDocsBtn");
  const docCards = document.querySelectorAll(".docs-card");

  if (!modal || !modalTitle || !modalBody || !closeBtn) return;

  function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function parseInlineMarkdown(str) {
    return str
      .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.*?)`/g, "<code>$1</code>")
      .replace(/\[(.*?)\]\((.*?)\)/g, '<a href="$2" target="_blank">$1</a>');
  }

  function parseMarkdownToHtml(md) {
    let html = '<div class="docs-content">';
    let inCodeBlock = false;
    let codeContent = '';
    let codeLang = '';
    let inList = false;

    const lines = md.split("\n");
    for (let line of lines) {
      const trimmed = line.trim();

      // Code blocks
      if (trimmed.startsWith("```")) {
        if (inCodeBlock) {
          html += `<pre><code class="language-${codeLang}">${escapeHtml(codeContent.trim())}</code></pre>`;
          inCodeBlock = false;
          codeContent = '';
        } else {
          if (inList) {
            html += "</ul>";
            inList = false;
          }
          inCodeBlock = true;
          codeLang = trimmed.slice(3);
        }
        continue;
      }

      if (inCodeBlock) {
        codeContent += line + "\n";
        continue;
      }

      // Horizontal rule
      if (trimmed === "---") {
        if (inList) { html += "</ul>"; inList = false; }
        html += "<hr>";
        continue;
      }

      // Headers
      if (line.startsWith("# ")) {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<h1>${parseInlineMarkdown(line.slice(2))}</h1>`;
        continue;
      }
      if (line.startsWith("## ")) {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<h2>${parseInlineMarkdown(line.slice(3))}</h2>`;
        continue;
      }
      if (line.startsWith("### ")) {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<h3>${parseInlineMarkdown(line.slice(4))}</h3>`;
        continue;
      }
      if (line.startsWith("#### ")) {
        if (inList) { html += "</ul>"; inList = false; }
        html += `<h4>${parseInlineMarkdown(line.slice(5))}</h4>`;
        continue;
      }

      // Alert/Callout blocks or Blockquotes
      if (trimmed.startsWith(">")) {
        if (inList) { html += "</ul>"; inList = false; }
        let quoteContent = trimmed.slice(1).trim();
        if (quoteContent.startsWith("[!NOTE]") || quoteContent.startsWith("[!IMPORTANT]") || quoteContent.startsWith("[!WARNING]") || quoteContent.startsWith("[!TIP]") || quoteContent.startsWith("[!CAUTION]")) {
          let alertClass = "alert-note";
          let alertLabel = "NOTE";
          if (quoteContent.startsWith("[!IMPORTANT]")) { alertClass = "alert-important"; alertLabel = "IMPORTANT"; }
          else if (quoteContent.startsWith("[!WARNING]")) { alertClass = "alert-warning"; alertLabel = "WARNING"; }
          else if (quoteContent.startsWith("[!CAUTION]")) { alertClass = "alert-warning"; alertLabel = "CAUTION"; }
          
          let alertText = quoteContent.replace(/^\[!.*?\]/, "").trim();
          html += `<div class="doc-alert ${alertClass}"><strong>${alertLabel}: </strong>${parseInlineMarkdown(alertText)}</div>`;
        } else {
          html += `<blockquote>${parseInlineMarkdown(quoteContent)}</blockquote>`;
        }
        continue;
      }

      // Lists
      if (line.startsWith("- ") || line.startsWith("* ")) {
        if (!inList) {
          html += "<ul>";
          inList = true;
        }
        html += `<li>${parseInlineMarkdown(line.slice(2))}</li>`;
        continue;
      }

      // Paragraphs
      if (trimmed === "") {
        if (inList) {
          html += "</ul>";
          inList = false;
        }
      } else {
        if (inList) {
          html += "</ul>";
          inList = false;
        }
        html += `<p>${parseInlineMarkdown(line)}</p>`;
      }
    }

    if (inList) {
      html += "</ul>";
    }
    html += "</div>";
    return html;
  }

  async function loadDoc(url, cardTitle) {
    modalBody.innerHTML = `
      <div class="docs-modal-loading">
        <div class="spinner"></div>
        <span>${document.documentElement.lang === "zh-CN" ? "正在加载文档..." : "Loading document..."}</span>
      </div>
    `;
    modalTitle.textContent = cardTitle;
    modal.classList.add("active");
    document.body.style.overflow = "hidden"; // Prevent scrolling behind modal

    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error("Network response was not ok");
      const mdText = await response.text();
      const htmlContent = parseMarkdownToHtml(mdText);
      modalBody.innerHTML = htmlContent;
    } catch (err) {
      console.warn("Dynamic doc fetch failed, falling back to direct link", err);
      // Fallback: close modal and redirect to raw markdown file
      closeModal();
      window.open(url, "_blank");
    }
  }

  function closeModal() {
    modal.classList.remove("active");
    document.body.style.overflow = "";
  }

  docCards.forEach((card) => {
    card.addEventListener("click", (e) => {
      e.preventDefault();
      const url = card.getAttribute("href");
      const title = card.querySelector("h3").textContent;
      loadDoc(url, title);
    });
  });

  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModal();
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("active")) {
      closeModal();
    }
  });
})();
