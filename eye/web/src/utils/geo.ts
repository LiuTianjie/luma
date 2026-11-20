const regionDisplay =
  typeof Intl !== "undefined" && typeof Intl.DisplayNames === "function"
    ? new Intl.DisplayNames(["zh-CN"], { type: "region" })
    : null;

export interface CountryMeta {
  code: string;
  name: string;
  label: string;
}

export function getCountryMeta(rawCode?: string | null): CountryMeta {
  const clean = (rawCode || "").trim().toUpperCase();
  if (!clean || clean === "(UNKNOWN)" || clean === "UNKNOWN" || clean === "XX") {
    return {
      code: "—",
      name: "未识别地区",
      label: "未识别地区",
    };
  }
  let name = clean;
  try {
    const resolved = regionDisplay?.of(clean);
    if (resolved) name = resolved;
  } catch {
    // ignore invalid region code
  }
  return {
    code: clean,
    name,
    label: name !== clean ? `${name} (${clean})` : clean,
  };
}

export interface VitalEvaluation {
  metric: string;
  title: string;
  description: string;
  rating: "good" | "needs-improvement" | "poor";
  badgeVariant: "success" | "warning" | "destructive";
  badgeLabel: string;
  formattedValue: string;
  thresholdHint: string;
  fillPercent: number;
}

export function evaluateVital(metricRaw: string, p75: number): VitalEvaluation {
  const m = metricRaw.toUpperCase();
  const variantOf = (r: "good" | "needs-improvement" | "poor") =>
    r === "good" ? ("success" as const) : r === "needs-improvement" ? ("warning" as const) : ("destructive" as const);

  if (m === "CLS") {
    const rating = p75 <= 0.1 ? "good" : p75 <= 0.25 ? "needs-improvement" : "poor";
    return {
      metric: "CLS",
      title: "累积布局偏移 (CLS)",
      description: "衡量页面视觉稳定性，防止元素意外位移引发误触",
      rating,
      badgeVariant: variantOf(rating),
      badgeLabel: rating === "good" ? "良好" : rating === "needs-improvement" ? "待改进" : "较差",
      formattedValue: p75.toFixed(3),
      thresholdHint: "良好 ≤ 0.10 · 较差 > 0.25",
      fillPercent: Math.min(100, Math.max(4, (p75 / 0.35) * 100)),
    };
  }
  if (m === "INP") {
    const rating = p75 <= 200 ? "good" : p75 <= 500 ? "needs-improvement" : "poor";
    return {
      metric: "INP",
      title: "交互响应延迟 (INP)",
      description: "用户点击或按键到浏览器下一帧渲染反馈的真实耗时",
      rating,
      badgeVariant: variantOf(rating),
      badgeLabel: rating === "good" ? "良好" : rating === "needs-improvement" ? "待改进" : "较差",
      formattedValue: `${Math.round(p75)}ms`,
      thresholdHint: "良好 ≤ 200ms · 较差 > 500ms",
      fillPercent: Math.min(100, Math.max(4, (p75 / 700) * 100)),
    };
  }
  if (m === "LCP") {
    const rating = p75 <= 2500 ? "good" : p75 <= 4000 ? "needs-improvement" : "poor";
    return {
      metric: "LCP",
      title: "最大内容绘制 (LCP)",
      description: "首屏主内容完成加载并呈现的时间",
      rating,
      badgeVariant: variantOf(rating),
      badgeLabel: rating === "good" ? "良好" : rating === "needs-improvement" ? "待改进" : "较差",
      formattedValue: p75 >= 1000 ? `${(p75 / 1000).toFixed(2)}s` : `${Math.round(p75)}ms`,
      thresholdHint: "良好 ≤ 2.5s · 较差 > 4.0s",
      fillPercent: Math.min(100, Math.max(4, (p75 / 5000) * 100)),
    };
  }
  if (m === "FCP") {
    const rating = p75 <= 1800 ? "good" : p75 <= 3000 ? "needs-improvement" : "poor";
    return {
      metric: "FCP",
      title: "首次内容绘制 (FCP)",
      description: "白屏结束、首个像素内容呈现的时间",
      rating,
      badgeVariant: variantOf(rating),
      badgeLabel: rating === "good" ? "良好" : rating === "needs-improvement" ? "待改进" : "较差",
      formattedValue: p75 >= 1000 ? `${(p75 / 1000).toFixed(2)}s` : `${Math.round(p75)}ms`,
      thresholdHint: "良好 ≤ 1.8s · 较差 > 3.0s",
      fillPercent: Math.min(100, Math.max(4, (p75 / 4000) * 100)),
    };
  }
  const rating = p75 <= 800 ? "good" : p75 <= 1800 ? "needs-improvement" : "poor";
  return {
    metric: m,
    title: m === "TTFB" ? "首字节响应 (TTFB)" : m,
    description: m === "TTFB" ? "DNS、TLS 与服务端首字节响应耗时" : "前端体验指标 P75 分位值",
    rating,
    badgeVariant: variantOf(rating),
    badgeLabel: rating === "good" ? "良好" : rating === "needs-improvement" ? "待改进" : "较差",
    formattedValue: `${Math.round(p75)}ms`,
    thresholdHint: "良好 ≤ 800ms · 较差 > 1800ms",
    fillPercent: Math.min(100, Math.max(4, (p75 / 2400) * 100)),
  };
}

export interface BreadcrumbCategoryMeta {
  key: string;
  label: string;
  tone: "click" | "nav" | "http" | "console" | "lifecycle" | "exception" | "default";
}

export function getBreadcrumbMeta(category?: string, level?: string): BreadcrumbCategoryMeta {
  const c = (category || "").toLowerCase();
  if (c === "exception" || level === "fatal") {
    return { key: "exception", label: "抛出异常", tone: "exception" };
  }
  if (c.startsWith("ui.") || c === "click" || c === "touch") {
    return { key: "ui.click", label: "用户点击", tone: "click" };
  }
  if (c === "navigation" || c === "route") {
    return { key: "navigation", label: "路由跳转", tone: "nav" };
  }
  if (c === "http" || c === "fetch" || c === "xhr") {
    return { key: "http", label: "网络请求", tone: "http" };
  }
  if (c === "console" || c === "log") {
    return { key: "console", label: c === "log" ? "业务日志" : "控制台", tone: "console" };
  }
  if (c === "lifecycle" || c === "app_state") {
    return { key: "lifecycle", label: "状态切换", tone: "lifecycle" };
  }
  return { key: c || "other", label: category || "其他", tone: "default" };
}
