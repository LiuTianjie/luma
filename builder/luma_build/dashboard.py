"""Luma Kumo-styled Web Console for the decoupled luma-build service.

Follows Luma Control's visual design system (Kumo OKLCH semantic tokens, tabular
numerals, quiet-dot / soft-pill StatePill badges, right-hand DetailDrawer, and
system-aware light/dark theming) with zero external runtime asset dependencies.
"""
from __future__ import annotations

import secrets
from typing import Tuple

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <rect width="32" height="32" rx="8" fill="#2563eb"/>
  <path d="M9 9h5v10h9v4H9V9z" fill="#ffffff"/>
  <circle cx="21.5" cy="11.5" r="2.5" fill="#93c5fd"/>
</svg>
"""


def render_dashboard_html(*, version: str) -> Tuple[str, str]:
    """Return ``(html_body, csp_nonce)`` with a fresh per-request nonce."""
    nonce = secrets.token_urlsafe(18)
    html = (
        _TEMPLATE.replace("__CSP_NONCE__", nonce)
        .replace("__LUMA_BUILD_VERSION__", version)
    )
    return html, nonce


_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <script nonce="__CSP_NONCE__">
    try {
      const savedScheme = localStorage.getItem("luma-build-color-scheme");
      if (savedScheme === "light" || savedScheme === "dark") {
        document.querySelector('meta[name="color-scheme"]').content = savedScheme;
      }
    } catch {}
  </script>
  <title>Luma Build · 构建与算力控制台</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style nonce="__CSP_NONCE__">
    :root {
      color-scheme: light dark;
      --font-sans: "Geist Variable", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

      --canvas-light: oklch(0.973 0 0);
      --canvas-dark: oklch(0.145 0 0);
      --bg-light: oklch(1 0 0);
      --bg-dark: oklch(0.18 0 0);
      --fg-light: oklch(0.21 0.006 285.9);
      --fg-dark: oklch(0.97 0 0);
      --card-light: oklch(1 0 0);
      --card-dark: oklch(0.195 0 0);
      --muted-light: oklch(0.97 0 0);
      --muted-dark: oklch(0.22 0 0);
      --muted-fg-light: oklch(0.48 0 0);
      --muted-fg-dark: oklch(0.72 0 0);
      --subtle-fg-light: oklch(0.556 0 0);
      --subtle-fg-dark: oklch(0.66 0 0);
      --accent-light: oklch(0.955 0 0);
      --accent-dark: oklch(0.255 0 0);
      --primary-light: oklch(0.5772 0.2324 260);
      --primary-dark: oklch(0.58 0.2 260);
      --primary-hover-light: oklch(0.488 0.243 264.4);
      --primary-hover-dark: oklch(0.63 0.19 258);
      --link-light: oklch(0.488 0.243 264.4);
      --link-dark: oklch(0.74 0.15 254.6);
      --border-light: oklch(0.145 0 0 / 0.1);
      --border-dark: oklch(1 0 0 / 0.11);
      --hairline-light: oklch(0.935 0 0);
      --hairline-dark: oklch(1 0 0 / 0.07);
      --input-light: oklch(0.145 0 0 / 0.14);
      --input-dark: oklch(1 0 0 / 0.16);
      --shadow-light: oklch(0 0 0 / 0.06);
      --shadow-dark: oklch(0 0 0 / 0.35);

      --success-light: oklch(0.596 0.145 163.2);
      --success-dark: oklch(0.7 0.16 163);
      --success-fg-light: oklch(0.432 0.095 166.9);
      --success-fg-dark: oklch(0.82 0.13 164);
      --success-soft-light: oklch(0.968 0.026 156.7);
      --success-soft-dark: oklch(0.255 0.04 160);

      --warning-light: oklch(0.739 0.177 58.2);
      --warning-dark: oklch(0.72 0.16 58);
      --warning-fg-light: oklch(0.5 0.13 50);
      --warning-fg-dark: oklch(0.83 0.14 70);
      --warning-soft-light: oklch(0.975 0.03 85);
      --warning-soft-dark: oklch(0.275 0.05 65);

      --danger-light: oklch(0.637 0.237 25.3);
      --danger-dark: oklch(0.65 0.21 24);
      --danger-fg-light: oklch(0.505 0.213 27.5);
      --danger-fg-dark: oklch(0.78 0.15 22.2);
      --danger-soft-light: oklch(0.965 0.018 17.7);
      --danger-soft-dark: oklch(0.27 0.06 25);

      --canvas: var(--canvas-light);
      --background: var(--bg-light);
      --foreground: var(--fg-light);
      --card: var(--card-light);
      --muted: var(--muted-light);
      --muted-foreground: var(--muted-fg-light);
      --subtle-foreground: var(--subtle-fg-light);
      --accent: var(--accent-light);
      --primary: var(--primary-light);
      --primary-hover: var(--primary-hover-light);
      --link: var(--link-light);
      --border: var(--border-light);
      --hairline: var(--hairline-light);
      --input: var(--input-light);
      --shadow-color: var(--shadow-light);
      --success: var(--success-light);
      --success-foreground: var(--success-fg-light);
      --success-soft: var(--success-soft-light);
      --warning: var(--warning-light);
      --warning-foreground: var(--warning-fg-light);
      --warning-soft: var(--warning-soft-light);
      --danger: var(--danger-light);
      --danger-foreground: var(--danger-fg-light);
      --danger-soft: var(--danger-soft-light);

      --radius: 0.5rem;
      --header-height: 3.5rem;
      accent-color: var(--primary);
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --canvas: var(--canvas-dark);
        --background: var(--bg-dark);
        --foreground: var(--fg-dark);
        --card: var(--card-dark);
        --muted: var(--muted-dark);
        --muted-foreground: var(--muted-fg-dark);
        --subtle-foreground: var(--subtle-fg-dark);
        --accent: var(--accent-dark);
        --primary: var(--primary-dark);
        --primary-hover: var(--primary-hover-dark);
        --link: var(--link-dark);
        --border: var(--border-dark);
        --hairline: var(--hairline-dark);
        --input: var(--input-dark);
        --shadow-color: var(--shadow-dark);
        --success: var(--success-dark);
        --success-foreground: var(--success-fg-dark);
        --success-soft: var(--success-soft-dark);
        --warning: var(--warning-dark);
        --warning-foreground: var(--warning-fg-dark);
        --warning-soft: var(--warning-soft-dark);
        --danger: var(--danger-dark);
        --danger-foreground: var(--danger-fg-dark);
        --danger-soft: var(--danger-soft-dark);
      }
    }

    :root:has(> head > meta[name="color-scheme"][content="dark"]) {
      color-scheme: dark;
    }
    :root:has(> head > meta[name="color-scheme"][content="light"]) {
      color-scheme: light;
    }

    @supports (color: light-dark(white, black)) {
      :root {
        --canvas: light-dark(var(--canvas-light), var(--canvas-dark));
        --background: light-dark(var(--bg-light), var(--bg-dark));
        --foreground: light-dark(var(--fg-light), var(--fg-dark));
        --card: light-dark(var(--card-light), var(--card-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --muted-foreground: light-dark(var(--muted-fg-light), var(--muted-fg-dark));
        --subtle-foreground: light-dark(var(--subtle-fg-light), var(--subtle-fg-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --primary: light-dark(var(--primary-light), var(--primary-dark));
        --primary-hover: light-dark(var(--primary-hover-light), var(--primary-hover-dark));
        --link: light-dark(var(--link-light), var(--link-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --hairline: light-dark(var(--hairline-light), var(--hairline-dark));
        --input: light-dark(var(--input-light), var(--input-dark));
        --shadow-color: light-dark(var(--shadow-light), var(--shadow-dark));
        --success: light-dark(var(--success-light), var(--success-dark));
        --success-foreground: light-dark(var(--success-fg-light), var(--success-fg-dark));
        --success-soft: light-dark(var(--success-soft-light), var(--success-soft-dark));
        --warning: light-dark(var(--warning-light), var(--warning-dark));
        --warning-foreground: light-dark(var(--warning-fg-light), var(--warning-fg-dark));
        --warning-soft: light-dark(var(--warning-soft-light), var(--warning-soft-dark));
        --danger: light-dark(var(--danger-light), var(--danger-dark));
        --danger-foreground: light-dark(var(--danger-fg-light), var(--danger-fg-dark));
        --danger-soft: light-dark(var(--danger-soft-light), var(--danger-soft-dark));
      }
    }

    *, *::before, *::after { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; font-family: var(--font-sans); }
    body {
      background: var(--canvas);
      color: var(--foreground);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
      font-size: 0.875rem;
      line-height: 1.5;
    }
    table, [data-numeric] { font-variant-numeric: tabular-nums; }
    code, pre, .mono { font-family: var(--font-mono); }

    /* Shell layout */
    .shell { display: flex; min-height: 100dvh; width: 100%; }
    .sidebar {
      width: 15rem;
      flex-shrink: 0;
      background: var(--background);
      border-inline-end: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      position: sticky;
      inset-block-start: 0;
      height: 100dvh;
    }
    .shell.collapsed .sidebar { width: 3.75rem; }
    .shell.collapsed .hide-collapsed { display: none !important; }
    .sidebar-header {
      height: var(--header-height);
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding-inline: 0.875rem;
      border-block-end: 1px solid var(--border);
    }
    .brand-mark {
      width: 2rem;
      height: 2rem;
      border-radius: 0.5rem;
      background: var(--primary);
      color: #fff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 0.95rem;
      flex-shrink: 0;
      box-shadow: 0 1px 2px var(--shadow-color);
    }
    .brand-meta { display: flex; flex-direction: column; min-width: 0; line-height: 1.15; }
    .brand-meta strong { font-size: 0.9375rem; font-weight: 600; letter-spacing: -0.01em; }
    .brand-meta span { font-size: 0.6875rem; color: var(--muted-foreground); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .sidebar-body { flex: 1; padding: 0.75rem 0.625rem; display: flex; flex-direction: column; gap: 0.875rem; overflow-y: auto; }
    .nav-group-label {
      padding: 0.25rem 0.625rem;
      font-size: 0.6875rem;
      font-weight: 500;
      color: var(--subtle-foreground);
      letter-spacing: 0.02em;
    }
    .nav-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
    .nav-btn {
      width: 100%;
      border: 0;
      background: transparent;
      color: var(--foreground);
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding: 0.45rem 0.625rem;
      border-radius: 0.45rem;
      font-size: 0.8125rem;
      font-weight: 500;
      cursor: pointer;
      text-align: start;
    }
    .nav-btn:hover { background: var(--accent); }
    .nav-btn[aria-current="page"] { background: var(--accent); font-weight: 600; }
    .nav-badge {
      margin-inline-start: auto;
      font-size: 0.6875rem;
      padding: 0.05rem 0.4rem;
      border-radius: 999px;
      background: var(--muted);
      color: var(--muted-foreground);
      border: 1px solid var(--border);
    }
    .sidebar-footer {
      padding: 0.625rem;
      border-block-start: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }

    .main-area { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    .topbar {
      height: var(--header-height);
      background: var(--background);
      border-block-end: 1px solid var(--border);
      padding-inline: clamp(1rem, 2.5vw, 2rem);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      position: sticky;
      inset-block-start: 0;
      z-index: 5;
    }
    .topbar-title { font-size: 0.875rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem; }
    .topbar-actions { display: flex; align-items: center; gap: 0.5rem; }

    .console-content {
      flex: 1;
      padding: 1.75rem clamp(1rem, 3vw, 2.5rem) 3rem;
    }
    .console-page {
      max-width: 1360px;
      margin-inline: auto;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }

    /* Page header */
    .page-header {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
    }
    .page-header h1 { margin: 0; font-size: 1.375rem; font-weight: 650; letter-spacing: -0.015em; }
    .page-header p { margin: 0.25rem 0 0; color: var(--muted-foreground); font-size: 0.8125rem; }
    .header-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; }

    /* Buttons & controls */
    button, input, select { font: inherit; }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.4rem;
      height: 2.125rem;
      padding-inline: 0.8rem;
      border-radius: 0.45rem;
      font-size: 0.8125rem;
      font-weight: 500;
      cursor: pointer;
      border: 1px solid var(--border);
      background: var(--background);
      color: var(--foreground);
      box-shadow: 0 1px 2px var(--shadow-color);
      white-space: nowrap;
      text-decoration: none;
    }
    .btn:hover { background: var(--accent); }
    .btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-primary {
      background: var(--primary);
      border-color: transparent;
      color: #fff;
    }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-danger {
      background: var(--danger-soft);
      border-color: color-mix(in oklch, var(--danger) 30%, transparent);
      color: var(--danger-foreground);
    }
    .btn-ghost { background: transparent; border-color: transparent; box-shadow: none; }
    .btn-ghost:hover { background: var(--accent); }
    .btn-sm { height: 1.75rem; padding-inline: 0.6rem; font-size: 0.75rem; }

    .input, .select {
      height: 2.125rem;
      padding-inline: 0.6875rem;
      border-radius: 0.45rem;
      border: 1px solid var(--input);
      background: var(--background);
      color: var(--foreground);
      font-size: 0.8125rem;
      outline: none;
    }
    .input:focus, .select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 24%, transparent);
    }

    /* Stat tiles */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
      gap: 0.75rem;
    }
    .stat-tile {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.875rem 1rem;
      box-shadow: 0 1px 2px var(--shadow-color);
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      min-width: 0;
    }
    .stat-label { font-size: 0.75rem; font-weight: 500; color: var(--muted-foreground); }
    .stat-value { font-size: 1.25rem; font-weight: 650; letter-spacing: -0.01em; }
    .stat-sub { font-size: 0.75rem; color: var(--subtle-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* Filter bar */
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }
    .search-box { flex: 1 1 14rem; max-width: 20rem; }
    .segmented {
      display: inline-flex;
      flex-wrap: wrap;
      padding: 2px;
      border-radius: 0.5rem;
      background: var(--muted);
      border: 1px solid var(--border);
      gap: 2px;
    }
    .seg-btn {
      border: 0;
      background: transparent;
      color: var(--muted-foreground);
      font-size: 0.75rem;
      font-weight: 500;
      padding: 0.28rem 0.65rem;
      border-radius: 0.35rem;
      cursor: pointer;
    }
    .seg-btn[aria-pressed="true"] {
      background: var(--background);
      color: var(--foreground);
      box-shadow: 0 1px 2px var(--shadow-color);
    }

    /* Card & Responsive Table */
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: 0 1px 2px var(--shadow-color);
      overflow: hidden;
    }
    .table-wrapper {
      overflow-x: auto;
      max-inline-size: 100%;
      container-type: inline-size;
      outline: none;
    }
    .table-wrapper:focus-visible {
      box-shadow: inset 0 0 0 2px var(--primary);
    }
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      text-align: start;
    }
    caption.sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
    thead {
      position: sticky;
      inset-block-start: 0;
      z-index: 1;
      background: var(--muted);
    }
    thead th {
      padding: 0.625rem 1rem;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--muted-foreground);
      border-block-end: 1px solid var(--border);
      white-space: nowrap;
    }
    tbody tr { cursor: pointer; transition: background-color 0.12s ease; }
    tbody tr.static-row { cursor: default; }
    tbody tr:hover { background: color-mix(in oklch, var(--accent) 65%, transparent); }
    tbody th, tbody td {
      padding: 0.75rem 1rem;
      border-block-end: 1px solid var(--hairline);
      vertical-align: middle;
      font-weight: 400;
    }
    tbody tr:last-child th, tbody tr:last-child td { border-block-end: 0; }

    .record-primary { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
    .record-title { color: var(--link); font-weight: 600; font-size: 0.875rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .record-sub { color: var(--muted-foreground); font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    @container (width < 640px) {
      .resp-table, .resp-table thead, .resp-table tbody, .resp-table tr, .resp-table th, .resp-table td {
        display: block;
      }
      .resp-table thead { display: none; }
      .resp-table tbody tr {
        padding: 0.5rem 0;
        border-block-end: 1px solid var(--border);
      }
      .resp-table tbody th, .resp-table tbody td {
        border-block-end: 0;
        padding: 0.35rem 1rem;
      }
      .resp-table tbody td[data-col="status"]::before { content: "状态："; color: var(--muted-foreground); font-size: 0.75rem; }
      .resp-table tbody td[data-col="meta"]::before { content: "架构 / 产物："; color: var(--muted-foreground); font-size: 0.75rem; }
      .resp-table tbody td[data-col="time"]::before { content: "时间："; color: var(--muted-foreground); font-size: 0.75rem; }
    }

    /* Luma StatePill badges */
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      font-size: 0.75rem;
      font-weight: 500;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      line-height: 1.35;
      white-space: nowrap;
    }
    .pill-dot::before {
      content: "";
      width: 0.42rem;
      height: 0.42rem;
      border-radius: 999px;
      background: currentColor;
    }
    .pill-success {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--foreground);
    }
    .pill-success::before { background: var(--success); }
    .pill-running {
      background: var(--success-soft);
      color: var(--success-foreground);
    }
    .pill-running::before { background: var(--success); }
    .pill-warning {
      background: var(--warning-soft);
      color: var(--warning-foreground);
    }
    .pill-danger {
      background: var(--danger-soft);
      color: var(--danger-foreground);
    }
    .pill-neutral {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--muted-foreground);
    }

    .empty-state {
      padding: 3rem 1.5rem;
      text-align: center;
      color: var(--muted-foreground);
    }
    .empty-state strong { display: block; color: var(--foreground); font-size: 0.9375rem; margin-block-end: 0.25rem; }

    /* Native <dialog> modals & right-side detail sheet (per modern-web-guidance) */
    dialog {
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--foreground);
      padding: 0;
      box-shadow: 0 12px 36px var(--shadow-color);
    }
    dialog:modal {
      overscroll-behavior: contain;
    }
    dialog::backdrop {
      background: oklch(0 0 0 / 0.42);
      backdrop-filter: blur(2px);
      overflow: hidden;
      overscroll-behavior: contain;
    }
    dialog.center-modal {
      width: min(34rem, calc(100vw - 2rem));
      max-height: calc(100dvh - 2rem);
      border-radius: calc(var(--radius) * 1.4);
      margin: auto;
    }
    dialog.center-modal[open] > form {
      display: flex;
      flex-direction: column;
      max-height: calc(100dvh - 2rem);
    }
    dialog.drawer-sheet {
      margin: 0 0 0 auto;
      width: min(46rem, 100vw);
      max-width: 100vw;
      height: 100dvh;
      max-height: 100dvh;
      border-radius: 0;
      border-inline-start: 1px solid var(--border);
    }
    dialog.drawer-sheet[open] {
      display: flex;
      flex-direction: column;
    }
    .dialog-header {
      padding: 1rem 1.25rem;
      border-block-end: 1px solid var(--border);
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
    }
    .dialog-header h2 { margin: 0; font-size: 1rem; font-weight: 600; }
    .dialog-header p { margin: 0.2rem 0 0; font-size: 0.75rem; color: var(--muted-foreground); }
    .dialog-body { padding: 1.25rem; display: flex; flex-direction: column; gap: 0.875rem; overflow-y: auto; }
    .drawer-body { flex: 1; min-height: 0; padding: 1rem 1.25rem; display: flex; flex-direction: column; gap: 1rem; overflow-y: auto; }
    .dialog-footer {
      padding: 0.875rem 1.25rem;
      border-block-start: 1px solid var(--border);
      display: flex;
      justify-content: flex-end;
      gap: 0.5rem;
    }
    .field { display: flex; flex-direction: column; gap: 0.35rem; }
    .field label { font-size: 0.75rem; font-weight: 500; color: var(--foreground); }
    .field small { font-size: 0.72rem; color: var(--muted-foreground); }
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
    .check-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; }
    .adv-box {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--muted);
      padding: 0.625rem 0.875rem;
    }
    .adv-summary {
      cursor: pointer;
      font-size: 0.78rem;
      font-weight: 500;
      color: var(--foreground);
      user-select: none;
    }
    .adv-body {
      margin-block-start: 0.75rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }

    .kv-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
      gap: 0.75rem;
      padding: 0.875rem;
      border-radius: var(--radius);
      background: var(--muted);
      border: 1px solid var(--border);
    }
    .kv-item dt { font-size: 0.7rem; color: var(--muted-foreground); }
    .kv-item dd { margin: 0.15rem 0 0; font-size: 0.8rem; word-break: break-all; }

    .log-viewer {
      color-scheme: dark;
      background: oklch(0.16 0.01 260);
      color: oklch(0.93 0 0);
      border-radius: var(--radius);
      border: 1px solid oklch(1 0 0 / 0.12);
      padding: 0.875rem 1rem;
      font-family: var(--font-mono);
      font-size: 0.75rem;
      line-height: 1.55;
      flex: 1;
      min-height: 18rem;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .log-line { padding-block: 1px; }
    .log-stage { color: oklch(0.75 0.14 250); margin-inline-end: 0.4rem; }
    .log-err { color: oklch(0.78 0.17 25); }

    .code-snippet {
      background: var(--muted);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.75rem;
      font-family: var(--font-mono);
      font-size: 0.75rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* Login screen */
    .auth-screen {
      min-height: 100dvh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }
    .auth-card {
      width: min(25rem, 100%);
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: calc(var(--radius) * 1.5);
      padding: 1.75rem;
      box-shadow: 0 8px 24px var(--shadow-color);
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    .banner-error {
      padding: 0.625rem 0.75rem;
      border-radius: 0.45rem;
      background: var(--danger-soft);
      color: var(--danger-foreground);
      font-size: 0.8rem;
    }
    .auth-brand { display: flex; align-items: center; gap: 0.65rem; }
    .auth-desc { margin: 0; font-size: 0.8125rem; color: var(--muted-foreground); }
    .w-full { width: 100%; }
    .stat-host { font-size: 0.95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .section-card { padding: 1.25rem; display: flex; flex-direction: column; gap: 0.75rem; }
    .row-between { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
    .section-title { font-size: 0.9375rem; }
    .min-w-0 { min-width: 0; }
    .drawer-actions { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; }
    .m-0 { margin: 0; }
    .log-heading { font-size: 0.8125rem; }
    .stream-hint { font-size: 0.72rem; color: var(--muted-foreground); }
    .stack-sm { display: flex; flex-direction: column; gap: 0.5rem; }
    .ok-note { font-size: 0.8rem; color: var(--success-foreground); }
    .cell-meta { font-size: 0.75rem; color: var(--muted-foreground); }
    .cell-time { font-size: 0.8rem; color: var(--muted-foreground); white-space: nowrap; }
    .cell-muted { color: var(--muted-foreground); font-size: 0.8rem; }
    .fw-semi { font-weight: 600; }
    [hidden] { display: none !important; }

    @media (max-width: 820px) {
      .sidebar { display: none; }
      .field-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <!-- Sign-in View (reuses Luma Control management token via SSO) -->
  <main id="auth-view" class="auth-screen">
    <form id="login-form" class="auth-card">
      <div class="auth-brand">
        <span class="brand-mark">L</span>
        <div class="brand-meta">
          <strong>Luma Build</strong>
          <span>v__LUMA_BUILD_VERSION__ · 独立构建与算力中心</span>
        </div>
      </div>
      <p class="auth-desc">
        直接输入你的 <strong>Luma 管理 Token</strong> 登录（服务端通过 Luma Control SSO 自动验签，与 <code>luma login</code> 共用同一套身份）。
      </p>
      <div id="login-error" class="banner-error" role="alert" hidden></div>
      <div class="field">
        <label for="login-token">访问令牌 (Token)</label>
        <input id="login-token" class="input" type="password" autocomplete="current-password" placeholder="粘贴 LUMA_DEPLOY_TOKEN…" required autofocus>
      </div>
      <label class="check-row">
        <input id="login-remember" type="checkbox" checked>
        <span>在此浏览器记住登录状态</span>
      </label>
      <button type="submit" class="btn btn-primary w-full">进入控制台</button>
    </form>
  </main>

  <!-- Authenticated Console Shell -->
  <div id="app-shell" class="shell" hidden>
    <aside class="sidebar" aria-label="主导航">
      <div class="sidebar-header">
        <span class="brand-mark">L</span>
        <div class="brand-meta hide-collapsed">
          <strong>Luma Build</strong>
          <span id="sidebar-subtitle">luma-build v__LUMA_BUILD_VERSION__</span>
        </div>
      </div>

      <div class="sidebar-body">
        <div>
          <div class="nav-group-label hide-collapsed">工作空间</div>
          <ul class="nav-list">
            <li>
              <button type="button" class="nav-btn" data-page="builds" aria-current="page">
                <span>◷</span>
                <span class="hide-collapsed">构建记录</span>
                <span id="nav-active-count" class="nav-badge hide-collapsed">0</span>
              </button>
            </li>
            <li>
              <button type="button" class="nav-btn" data-page="runners">
                <span>⚡</span>
                <span class="hide-collapsed">Runner 算力池</span>
                <span id="nav-runner-count" class="nav-badge hide-collapsed">0</span>
              </button>
            </li>
          </ul>
        </div>

        <div>
          <div class="nav-group-label hide-collapsed">平台管理</div>
          <ul class="nav-list">
            <li>
              <button type="button" class="nav-btn" data-page="settings">
                <span>⚙</span>
                <span class="hide-collapsed">凭据与配置</span>
              </button>
            </li>
          </ul>
        </div>
      </div>

      <div class="sidebar-footer">
        <button type="button" id="collapse-btn" class="nav-btn" title="收起或展开侧边栏">
          <span>⇤</span>
          <span class="hide-collapsed">收起侧边栏</span>
        </button>
      </div>
    </aside>

    <div class="main-area">
      <header class="topbar">
        <div class="topbar-title">
          <span id="topbar-page-name">构建记录</span>
        </div>
        <div class="topbar-actions">
          <a id="control-link" class="btn btn-sm btn-ghost" href="#" target="_blank" rel="noopener" hidden>↗ Luma 控制台</a>
          <button type="button" id="theme-toggle" class="btn btn-sm" aria-label="切换亮暗主题">◐ 主题</button>
          <button type="button" id="logout-btn" class="btn btn-sm">退出</button>
        </div>
      </header>

      <div class="console-content">
        <!-- PAGE 1: Build Timeline -->
        <section id="page-builds" class="console-page">
          <div class="page-header">
            <div>
              <h1>构建与推送时间线</h1>
              <p>按仓库、分支和状态查询；点击任意记录打开抽屉实时查看 BuildKit 日志与部署进度。</p>
            </div>
            <div class="header-actions">
              <button type="button" id="refresh-builds-btn" class="btn">↻ 刷新</button>
              <button type="button" id="open-submit-btn" class="btn btn-primary">+ 从 Git 构建</button>
            </div>
          </div>

          <div class="stats-grid">
            <div class="stat-tile">
              <span class="stat-label">排队 / 构建中</span>
              <strong id="stat-active" class="stat-value" data-numeric>0</strong>
              <span id="stat-oldest" class="stat-sub">队列空闲</span>
            </div>
            <div class="stat-tile">
              <span class="stat-label">在线 Runner 算力</span>
              <strong id="stat-runners" class="stat-value" data-numeric>0 / 0</strong>
              <span id="stat-platforms" class="stat-sub">等待心跳接入</span>
            </div>
            <div class="stat-tile">
              <span class="stat-label">累计成功 / 失败</span>
              <strong id="stat-outcomes" class="stat-value" data-numeric>0 / 0</strong>
              <span class="stat-sub">自动保留最近 500 条记录</span>
            </div>
            <div class="stat-tile">
              <span class="stat-label">目标镜像仓库 (Registry)</span>
              <strong id="stat-registry" class="stat-value stat-host">-</strong>
              <span id="stat-namespace" class="stat-sub">-</span>
            </div>
          </div>

          <div class="filter-bar" role="search" aria-label="筛选构建记录">
            <input id="filter-repo" class="input search-box" type="search" placeholder="搜索仓库或分支名称…">
            <div id="status-segments" class="segmented" role="group" aria-label="构建状态筛选">
              <button type="button" class="seg-btn" data-status="" aria-pressed="true">全部</button>
              <button type="button" class="seg-btn" data-status="queued" aria-pressed="false">排队中</button>
              <button type="button" class="seg-btn" data-status="building" aria-pressed="false">运行</button>
              <button type="button" class="seg-btn" data-status="deploying" aria-pressed="false">部署中</button>
              <button type="button" class="seg-btn" data-status="succeeded" aria-pressed="false">成功</button>
              <button type="button" class="seg-btn" data-status="failed" aria-pressed="false">失败</button>
              <button type="button" class="seg-btn" data-status="canceled" aria-pressed="false">已取消</button>
            </div>
          </div>

          <div class="card">
            <div tabindex="0" role="region" aria-labelledby="builds-caption" class="table-wrapper">
              <table class="resp-table">
                <caption id="builds-caption" class="sr-only">构建任务时间线列表</caption>
                <thead>
                  <tr>
                    <th>记录</th>
                    <th>状态</th>
                    <th>架构 / Runner</th>
                    <th>创建时间</th>
                  </tr>
                </thead>
                <tbody id="builds-tbody"></tbody>
              </table>
            </div>
          </div>
        </section>

        <!-- PAGE 2: Runners Pool -->
        <section id="page-runners" class="console-page" hidden>
          <div class="page-header">
            <div>
              <h1>Runner 算力池</h1>
              <p>所有算力机通过反向长轮询连接本服务，无需公网入站端口、无需安装 Nomad，与 Luma 控制面完全解耦。</p>
            </div>
            <div class="header-actions">
              <button type="button" id="refresh-runners-btn" class="btn">↻ 刷新</button>
              <button type="button" id="open-token-btn" class="btn btn-primary">+ 新建 Runner Token</button>
            </div>
          </div>

          <div class="card">
            <div tabindex="0" role="region" aria-label="已注册 Runner 列表" class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Runner 名称</th>
                    <th>状态</th>
                    <th>原生架构</th>
                    <th>并发槽位</th>
                    <th>权重 / 缓存策略</th>
                    <th>最近心跳</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody id="runners-tbody"></tbody>
              </table>
            </div>
          </div>
        </section>

        <!-- PAGE 3: Credentials & Config -->
        <section id="page-settings" class="console-page" hidden>
          <div class="page-header">
            <div>
              <h1>凭据与服务配置</h1>
              <p>推荐通过 <code>luma secret set KEY VALUE --scope luma-build</code> 注入环境密钥；也可在此管理动态 Git 与 Registry 凭据。</p>
            </div>
          </div>

          <div id="settings-summary" class="kv-grid"></div>

          <div class="card section-card">
            <div class="row-between">
              <strong class="section-title">动态 Git 拉取凭据</strong>
              <button type="button" id="open-git-cred-btn" class="btn btn-sm">+ 添加 Git 凭据</button>
            </div>
            <div tabindex="0" role="region" aria-label="Git 凭据列表" class="table-wrapper">
              <table>
                <thead>
                  <tr><th>凭据 ID</th><th>Git 域名</th><th>用户名</th><th>路径前缀</th><th>操作</th></tr>
                </thead>
                <tbody id="git-creds-tbody"></tbody>
              </table>
            </div>
          </div>

          <div class="card section-card">
            <div class="row-between">
              <strong class="section-title">动态 OCI Registry Push 凭据</strong>
              <button type="button" id="open-reg-cred-btn" class="btn btn-sm">+ 添加 Registry 凭据</button>
            </div>
            <div tabindex="0" role="region" aria-label="Registry 凭据列表" class="table-wrapper">
              <table>
                <thead>
                  <tr><th>Registry 域名</th><th>用户名</th><th>协议</th><th>操作</th></tr>
                </thead>
                <tbody id="reg-creds-tbody"></tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>

  <!-- Right-side Slide-over Detail & Live Log Drawer -->
  <dialog id="detail-drawer" class="drawer-sheet" closedby="any" aria-labelledby="drawer-title">
    <div class="dialog-header">
      <div class="min-w-0">
        <h2 id="drawer-title">-</h2>
        <p id="drawer-subtitle" class="mono">-</p>
      </div>
      <div class="drawer-actions">
        <button type="button" id="drawer-retry-btn" class="btn btn-sm">↻ 重试</button>
        <button type="button" id="drawer-cancel-btn" class="btn btn-sm btn-danger" hidden>✕ 取消构建</button>
        <form method="dialog" class="m-0">
          <button type="submit" class="btn btn-sm btn-ghost" aria-label="关闭详情抽屉">✕</button>
        </form>
      </div>
    </div>
    <div class="drawer-body">
      <dl id="drawer-kv" class="kv-grid m-0"></dl>
      <div class="row-between">
        <strong class="log-heading">实时构建与部署日志</strong>
        <span id="drawer-stream-state" class="stream-hint"></span>
      </div>
      <div id="drawer-logs" class="log-viewer" role="log" aria-live="polite"></div>
    </div>
  </dialog>

  <!-- Modal: Submit Git Build -->
  <dialog id="submit-dialog" class="center-modal" closedby="any" aria-labelledby="submit-dialog-title">
    <form id="submit-build-form">
      <div class="dialog-header">
        <div>
          <h2 id="submit-dialog-title">从 Git 提交构建</h2>
          <p>构建完成后默认自动推送到 Registry 并调用 Luma Control 部署。</p>
        </div>
        <button type="button" data-close-dialog="submit-dialog" class="btn btn-sm btn-ghost" aria-label="关闭">✕</button>
      </div>
      <div class="dialog-body">
        <div id="submit-error" class="banner-error" role="alert" hidden></div>
        <div class="field">
          <label for="sub-source">Git 仓库地址或 owner/repo</label>
          <input id="sub-source" class="input" placeholder="例如 https://git.example.com/org/app.git 或 acme/web" required autofocus>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="sub-ref">分支 / Tag / Commit</label>
            <input id="sub-ref" class="input" value="main" required>
          </div>
          <div class="field">
            <label for="sub-platform">目标架构</label>
            <select id="sub-platform" class="select">
              <option value="">自动识别（按目标节点 / 清单 / 历史记录路由）</option>
              <option value="linux/amd64">linux/amd64 (x86_64)</option>
              <option value="linux/arm64">linux/arm64 (Apple Silicon / ARM)</option>
            </select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="sub-runner">指定 Runner（可选）</label>
            <input id="sub-runner" class="input" placeholder="留空则按架构与热缓存自动调度">
          </div>
          <div class="field">
            <label for="sub-context">子目录 Context（可选）</label>
            <input id="sub-context" class="input" placeholder="默认仓库根目录 .">
          </div>
        </div>
        <details class="adv-box">
          <summary class="adv-summary">高级与单仓多服选项（Provider / 清单路径 / 部署覆盖）</summary>
          <div class="adv-body">
            <div class="field-row">
              <div class="field">
                <label for="sub-provider-id">Git 凭据 Provider ID（可选）</label>
                <input id="sub-provider-id" class="input" placeholder="例如 gitea:corp 或 github:main">
              </div>
              <div class="field">
                <label for="sub-dockerfile">Dockerfile 路径（可选）</label>
                <input id="sub-dockerfile" class="input" placeholder="默认 Dockerfile">
              </div>
            </div>
            <div class="field-row">
              <div class="field">
                <label for="sub-manifest-path">单服务清单路径（可选）</label>
                <input id="sub-manifest-path" class="input" placeholder="例如 luma.prod.api.yml">
              </div>
              <div class="field">
                <label for="sub-compose-sidecar">Compose 清单路径（可选）</label>
                <input id="sub-compose-sidecar" class="input" placeholder="例如 luma.prod.compose.yml">
              </div>
            </div>
            <div class="field-row">
              <div class="field">
                <label for="sub-region">覆盖目标 Region（可选）</label>
                <input id="sub-region" class="input" placeholder="例如 cn 或 hk">
              </div>
              <div class="field">
                <label for="sub-node">覆盖目标节点 Node（可选）</label>
                <input id="sub-node" class="input" placeholder="例如 manager 或 arm-worker">
              </div>
            </div>
          </div>
        </details>
        <label class="check-row">
          <input id="sub-no-deploy" type="checkbox">
          <span>仅构建并推送镜像（不触发 Luma Control 部署）</span>
        </label>
        <label class="check-row">
          <input id="sub-no-cache" type="checkbox">
          <span>禁用缓存重新全量编译 (<code>--no-cache</code>)</span>
        </label>
      </div>
      <div class="dialog-footer">
        <button type="button" data-close-dialog="submit-dialog" class="btn">取消</button>
        <button type="submit" id="submit-build-btn" class="btn btn-primary">立即构建</button>
      </div>
    </form>
  </dialog>

  <!-- Modal: Create Runner Token -->
  <dialog id="token-dialog" class="center-modal" closedby="any" aria-labelledby="token-dialog-title">
    <form id="create-token-form">
      <div class="dialog-header">
        <div>
          <h2 id="token-dialog-title">签发 Runner 接入令牌</h2>
          <p>生成的 <code>lbr_...</code> 令牌仅用于拉取构建任务，无管理员及密钥读取权限。</p>
        </div>
        <button type="button" data-close-dialog="token-dialog" class="btn btn-sm btn-ghost" aria-label="关闭">✕</button>
      </div>
      <div class="dialog-body">
        <div id="token-error" class="banner-error" role="alert" hidden></div>
        <div class="field">
          <label for="tok-name">Runner 名称</label>
          <input id="tok-name" class="input" placeholder="例如 x86-builder、m4、air3" required autofocus>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="tok-weight">调度权重 (1-1000)</label>
            <input id="tok-weight" class="input" type="number" value="100" min="1" max="1000">
            <small>空闲时高权重机器优先接单</small>
          </div>
          <div class="field">
            <label for="tok-concurrency">最大并发构建数</label>
            <input id="tok-concurrency" class="input" type="number" value="1" min="1" max="16">
          </div>
        </div>
        <div class="field">
          <label for="tok-cache">远端 Registry 缓存策略</label>
          <select id="tok-cache" class="select">
            <option value="read-write">read-write（推荐云主机：拉取并回推远端缓存）</option>
            <option value="read-only">read-only（推荐家庭慢上行：本地热缓存 + 只拉远端缓存）</option>
            <option value="local-only">local-only（仅使用本地磁盘 BuildKit 缓存）</option>
          </select>
        </div>
        <label class="check-row">
          <input id="tok-rotate" type="checkbox">
          <span>若同名 Runner 已存在则轮换旧令牌</span>
        </label>
        <div id="token-result-box" class="stack-sm" hidden>
          <strong class="ok-note">✓ 已生成令牌（仅显示一次，请妥善复制）：</strong>
          <div id="token-cli-snippet" class="code-snippet"></div>
          <div id="token-docker-snippet" class="code-snippet"></div>
        </div>
      </div>
      <div class="dialog-footer">
        <button type="button" data-close-dialog="token-dialog" class="btn">关闭</button>
        <button type="submit" class="btn btn-primary">生成令牌</button>
      </div>
    </form>
  </dialog>

  <!-- Modal: Add Git Credential -->
  <dialog id="git-cred-dialog" class="center-modal" closedby="any" aria-labelledby="git-cred-title">
    <form id="git-cred-form">
      <div class="dialog-header">
        <div><h2 id="git-cred-title">保存 Git 拉取凭据</h2></div>
        <button type="button" data-close-dialog="git-cred-dialog" class="btn btn-sm btn-ghost" aria-label="关闭">✕</button>
      </div>
      <div class="dialog-body">
        <div id="git-cred-error" class="banner-error" role="alert" hidden></div>
        <div class="field"><label for="gc-id">凭据标识 ID</label><input id="gc-id" class="input" placeholder="例如 gitea:corp 或 github:main" required autofocus></div>
        <div class="field"><label for="gc-host">Git 域名</label><input id="gc-host" class="input" placeholder="例如 git.example.com 或 github.com" required></div>
        <div class="field"><label for="gc-user">用户名</label><input id="gc-user" class="input" value="x-access-token" required></div>
        <div class="field"><label for="gc-token">Personal Access Token</label><input id="gc-token" class="input" type="password" required></div>
      </div>
      <div class="dialog-footer">
        <button type="button" data-close-dialog="git-cred-dialog" class="btn">取消</button>
        <button type="submit" class="btn btn-primary">保存</button>
      </div>
    </form>
  </dialog>

  <!-- Modal: Add Registry Credential -->
  <dialog id="reg-cred-dialog" class="center-modal" closedby="any" aria-labelledby="reg-cred-title">
    <form id="reg-cred-form">
      <div class="dialog-header">
        <div><h2 id="reg-cred-title">保存 OCI Registry Push 凭据</h2></div>
        <button type="button" data-close-dialog="reg-cred-dialog" class="btn btn-sm btn-ghost" aria-label="关闭">✕</button>
      </div>
      <div class="dialog-body">
        <div id="reg-cred-error" class="banner-error" role="alert" hidden></div>
        <div class="field"><label for="rc-host">Registry 域名</label><input id="rc-host" class="input" placeholder="例如 crpi-xxx.cn-beijing.personal.cr.aliyuncs.com" required autofocus></div>
        <div class="field"><label for="rc-user">Push 用户名</label><input id="rc-user" class="input" required></div>
        <div class="field"><label for="rc-pass">Push 密码</label><input id="rc-pass" class="input" type="password" required></div>
      </div>
      <div class="dialog-footer">
        <button type="button" data-close-dialog="reg-cred-dialog" class="btn">取消</button>
        <button type="submit" class="btn btn-primary">保存</button>
      </div>
    </form>
  </dialog>

  <script nonce="__CSP_NONCE__">
  (() => {
    const TOKEN_KEY = "luma-build-auth-token";
    const SCHEME_KEY = "luma-build-color-scheme";
    const state = {
      token: "",
      page: "builds",
      statusFilter: "",
      repoFilter: "",
      statusData: null,
      builds: [],
      selectedBuildId: "",
      streamAbort: null,
      pollTimer: null,
    };

    // Safe DOM construction helper (never uses innerHTML for untrusted data)
    function el(tag, attrs, ...children) {
      const node = document.createElement(tag);
      if (attrs) {
        for (const [k, v] of Object.entries(attrs)) {
          if (v === null || v === undefined || v === false) continue;
          if (k === "className") node.className = v;
          else if (k === "onclick") node.addEventListener("click", v);
          else if (v === true) node.setAttribute(k, "");
          else node.setAttribute(k, String(v));
        }
      }
      for (const child of children) {
        if (child === null || child === undefined || child === false) continue;
        node.append(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
      }
      return node;
    }

    // Light-dismiss fallback for browsers without native <dialog closedby> (e.g. Safari)
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (ev, dlg) => {
        const r = dlg.getBoundingClientRect();
        return ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom;
      };
      for (const dlg of document.querySelectorAll('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dlg.addEventListener("pointerdown", (ev) => { pressedOutside = ev.target === dlg && outside(ev, dlg); });
        dlg.addEventListener("pointerup", (ev) => { if (pressedOutside && ev.target === dlg && outside(ev, dlg)) dlg.close(); });
      }
    }
    for (const dlg of document.querySelectorAll("dialog")) {
      dlg.addEventListener("close", () => { dlg.returnValue = ""; });
    }
    for (const btn of document.querySelectorAll("[data-close-dialog]")) {
      btn.addEventListener("click", () => {
        const target = document.getElementById(btn.getAttribute("data-close-dialog"));
        if (target) target.close();
      });
    }

    // Two-state color-scheme toggle synced with <meta name="color-scheme">
    const schemeMeta = document.querySelector('meta[name="color-scheme"]');
    function currentRenderedScheme() {
      const content = schemeMeta.content;
      if (content === "dark" || content === "light") return content;
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.getElementById("theme-toggle").addEventListener("click", () => {
      const target = currentRenderedScheme() === "dark" ? "light" : "dark";
      const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      const sysScheme = sysDark ? "dark" : "light";
      try {
        if (target === sysScheme) {
          localStorage.removeItem(SCHEME_KEY);
          schemeMeta.content = "light dark";
        } else {
          localStorage.setItem(SCHEME_KEY, target);
          schemeMeta.content = target;
        }
      } catch {
        schemeMeta.content = target;
      }
    });
    window.addEventListener("storage", (ev) => {
      if (ev.key === SCHEME_KEY) {
        schemeMeta.content = ev.newValue === "light" || ev.newValue === "dark" ? ev.newValue : "light dark";
      }
    });

    function fmtTime(epoch) {
      if (!epoch) return "-";
      const d = new Date(Number(epoch) * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function renderStatusPill(status) {
      const map = {
        queued: ["排队中", "pill pill-warning"],
        building: ["运行", "pill pill-dot pill-running"],
        queued_deploy: ["待部署", "pill pill-warning"],
        deploying: ["部署中", "pill pill-warning"],
        succeeded: ["成功", "pill pill-dot pill-success"],
        failed: ["失败", "pill pill-danger"],
        canceled: ["已取消", "pill pill-dot pill-neutral"],
      };
      const [label, cls] = map[status] || [status || "-", "pill pill-neutral"];
      return el("span", { className: cls }, label);
    }

    async function api(path, opts = {}) {
      const res = await fetch(path, {
        method: opts.method || "GET",
        headers: {
          "Authorization": `Bearer ${state.token}`,
          "Accept": "application/json",
          ...(opts.body ? { "Content-Type": "application/json" } : {}),
        },
        body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 401) {
        signOut();
        throw new Error(data.error || "登录令牌无效或已过期");
      }
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      return data;
    }

    function loadStoredToken() {
      try {
        return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || "";
      } catch {
        return "";
      }
    }

    function signOut() {
      state.token = "";
      if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
      if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
      try { sessionStorage.removeItem(TOKEN_KEY); localStorage.removeItem(TOKEN_KEY); } catch {}
      document.getElementById("app-shell").hidden = true;
      document.getElementById("auth-view").hidden = false;
    }

    document.getElementById("logout-btn").addEventListener("click", signOut);
    document.getElementById("collapse-btn").addEventListener("click", () => {
      document.getElementById("app-shell").classList.toggle("collapsed");
    });

    // Navigation
    const pageNames = { builds: "构建记录", runners: "Runner 算力池", settings: "凭据与配置" };
    for (const btn of document.querySelectorAll(".nav-btn[data-page]")) {
      btn.addEventListener("click", () => {
        state.page = btn.getAttribute("data-page");
        for (const b of document.querySelectorAll(".nav-btn[data-page]")) {
          if (b === btn) b.setAttribute("aria-current", "page");
          else b.removeAttribute("aria-current");
        }
        document.getElementById("topbar-page-name").textContent = pageNames[state.page] || "";
        document.getElementById("page-builds").hidden = state.page !== "builds";
        document.getElementById("page-runners").hidden = state.page !== "runners";
        document.getElementById("page-settings").hidden = state.page !== "settings";
        refreshAll();
      });
    }

    // Login handler
    document.getElementById("login-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("login-error");
      errEl.hidden = true;
      const tok = document.getElementById("login-token").value.trim();
      const remember = document.getElementById("login-remember").checked;
      state.token = tok;
      try {
        await api("/v1/status");
        try {
          sessionStorage.setItem(TOKEN_KEY, tok);
          if (remember) localStorage.setItem(TOKEN_KEY, tok);
        } catch {}
        document.getElementById("login-token").value = "";
        enterShell();
      } catch (err) {
        errEl.textContent = err.message || "验证失败";
        errEl.hidden = false;
      }
    });

    function enterShell() {
      document.getElementById("auth-view").hidden = true;
      document.getElementById("app-shell").hidden = false;
      refreshAll();
      if (!state.pollTimer) {
        state.pollTimer = setInterval(() => {
          if (document.visibilityState === "visible" && state.token) refreshAll();
        }, 4000);
      }
    }

    async function refreshAll() {
      try {
        const [st, bl] = await Promise.all([
          api("/v1/status"),
          api(buildListUrl()),
        ]);
        state.statusData = st;
        state.builds = bl.builds || [];
        renderSummary(st);
        renderBuildsTable(state.builds);
        renderRunnersTable(st.runners || []);
        if (state.page === "settings") {
          const [gc, rc] = await Promise.all([api("/v1/credentials/git"), api("/v1/credentials/registry")]);
          renderSettings(st, gc.credentials || [], rc.credentials || []);
        }
      } catch {}
    }

    function buildListUrl() {
      const p = new URLSearchParams({ limit: "80" });
      if (state.statusFilter) p.set("status", state.statusFilter);
      if (state.repoFilter) p.set("repo", state.repoFilter);
      return `/v1/builds?${p}`;
    }

    function renderSummary(st) {
      const c = st.buildCounts || {};
      const active = (c.queued || 0) + (c.building || 0) + (c.queued_deploy || 0) + (c.deploying || 0);
      const succ = c.succeeded || 0;
      const fail = c.failed || 0;
      const runners = (st.runners || []).filter((r) => !r.revoked);
      const online = runners.filter((r) => r.online);
      document.getElementById("nav-active-count").textContent = String(active);
      document.getElementById("nav-runner-count").textContent = `${online.length}/${runners.length}`;
      document.getElementById("stat-active").textContent = String(active);
      document.getElementById("stat-oldest").textContent = st.oldestQueuedSeconds > 0 ? `最长排队 ${st.oldestQueuedSeconds}s` : "队列畅通";
      document.getElementById("stat-runners").textContent = `${online.length} / ${runners.length}`;
      const plats = [...new Set(online.map((r) => r.nativePlatform).filter(Boolean))];
      document.getElementById("stat-platforms").textContent = plats.length ? plats.join(", ") : "暂无在线节点";
      document.getElementById("stat-outcomes").textContent = `${succ} / ${fail}`;
      const reg = st.registry || {};
      document.getElementById("stat-registry").textContent = reg.registryHost || "未配置默认仓库";
      document.getElementById("stat-namespace").textContent = reg.namespace ? `命名空间: ${reg.namespace} (自动扁平化)` : "多级路径模式";

      const ctl = st.controlTarget || {};
      const ctlLink = document.getElementById("control-link");
      if (ctl.endpoint) {
        ctlLink.href = ctl.endpoint;
        ctlLink.hidden = false;
      }
    }

    function renderBuildsTable(builds) {
      const tbody = document.getElementById("builds-tbody");
      tbody.replaceChildren();
      if (!builds.length) {
        tbody.append(
          el("tr", { className: "static-row" },
            el("td", { colspan: "4" },
              el("div", { className: "empty-state" },
                el("strong", null, "暂无匹配的构建记录"),
                "点击右上角「+ 从 Git 构建」或在终端执行 luma builder submit 发起首次构建。"
              )
            )
          )
        );
        return;
      }
      for (const b of builds) {
        const sha = (b.result && b.result.shortSha) ? b.result.shortSha : b.ref;
        const runnerLabel = b.runner || (b.pinnedRunner ? `指定:${b.pinnedRunner}` : "待分配");
        const subtitle = `构建 · ${sha} · ${runnerLabel}${b.message ? " · " + b.message : ""}`;
        const platText = `${(b.platforms || []).join(", ")}${b.deployEnabled ? " → 自动部署" : " → 仅推镜像"}`;
        const tr = el("tr", { onclick: () => openBuildDrawer(b.id) },
          el("th", { scope: "row" },
            el("div", { className: "record-primary" },
              el("span", { className: "record-title" }, b.repoSlug),
              el("span", { className: "record-sub mono" }, subtitle)
            )
          ),
          el("td", { "data-col": "status" }, renderStatusPill(b.status)),
          el("td", { "data-col": "meta", className: "mono cell-meta" }, platText),
          el("td", { "data-col": "time", "data-numeric": "", className: "cell-time" }, fmtTime(b.createdAt))
        );
        tbody.append(tr);
      }
    }

    function renderRunnersTable(runners) {
      const tbody = document.getElementById("runners-tbody");
      tbody.replaceChildren();
      if (!runners.length) {
        tbody.append(
          el("tr", { className: "static-row" },
            el("td", { colspan: "7" },
              el("div", { className: "empty-state" },
                el("strong", null, "尚未注册任何 Runner"),
                "点击右上角「+ 新建 Runner Token」即可把任意 Linux 主机或家里 Mac 接入编译池。"
              )
            )
          )
        );
        return;
      }
      for (const r of runners) {
        const pill = r.online
          ? el("span", { className: "pill pill-dot pill-success" }, "在线")
          : el("span", { className: "pill pill-dot pill-neutral" }, "离线");
        const deleteBtn = el("button", {
          type: "button",
          className: "btn btn-sm btn-danger",
          onclick: async () => {
            if (!confirm(`确定删除 Runner "${r.name}"？其 token 将失效，正在构建及固定等待它的任务会取消，历史记录保留。`)) return;
            await api(`/v1/runners/${encodeURIComponent(r.name)}`, { method: "DELETE" });
            refreshAll();
          },
        }, "删除");
        tbody.append(
          el("tr", { className: "static-row" },
            el("th", { scope: "row", className: "mono fw-semi" }, r.name),
            el("td", null, pill),
            el("td", { className: "mono" }, r.nativePlatform || "-"),
            el("td", { "data-numeric": "" }, `${r.activeBuilds || 0} / ${r.maxConcurrency || 1}`),
            el("td", { className: "mono cell-meta" }, `weight=${r.weight} · ${r.cacheMode}`),
            el("td", { "data-numeric": "", className: "cell-time" }, fmtTime(r.lastSeenAt)),
            el("td", null, deleteBtn)
          )
        );
      }
    }

    function renderSettings(st, gitCreds, regCreds) {
      const box = document.getElementById("settings-summary");
      box.replaceChildren();
      const items = [
        ["认证模式", st.authMode || "-"],
        ["Luma Control 部署目标", (st.controlTarget && st.controlTarget.endpoint) || "未启用（仅构建模式）"],
        ["默认 Pull Registry", (st.registry && st.registry.registryHost) || "-"],
        ["默认 Push Registry", (st.registry && st.registry.pushHost) || "-"],
        ["Registry 命名空间", (st.registry && st.registry.namespace) || "-"],
        ["热缓存亲和等待窗口", `${st.affinityWaitSeconds ?? 20} 秒`],
      ];
      for (const [k, v] of items) {
        box.append(el("div", { className: "kv-item" }, el("dt", null, k), el("dd", { className: "mono" }, v)));
      }

      const gt = document.getElementById("git-creds-tbody");
      gt.replaceChildren();
      if (!gitCreds.length) {
        gt.append(el("tr", { className: "static-row" }, el("td", { colspan: "5", className: "cell-muted" }, "当前使用环境变量 Scoped Secrets（GITHUB_TOKEN / GITEA_TOKEN），暂无额外动态条目。")));
      } else {
        for (const c of gitCreds) {
          gt.append(el("tr", { className: "static-row" },
            el("th", { scope: "row", className: "mono" }, c.id),
            el("td", { className: "mono" }, c.host),
            el("td", { className: "mono" }, c.username),
            el("td", { className: "mono" }, c.pathPrefix || "*"),
            el("td", null, el("button", {
              type: "button",
              className: "btn btn-sm btn-danger",
              onclick: async () => {
                await api(`/v1/credentials/git/${encodeURIComponent(c.id)}`, { method: "DELETE" });
                refreshAll();
              },
            }, "删除"))
          ));
        }
      }

      const rt = document.getElementById("reg-creds-tbody");
      rt.replaceChildren();
      if (!regCreds.length) {
        rt.append(el("tr", { className: "static-row" }, el("td", { colspan: "4", className: "cell-muted" }, "当前使用环境变量 Scoped Secrets（LUMA_BUILD_REGISTRY_USERNAME / PASSWORD），暂无额外动态条目。")));
      } else {
        for (const c of regCreds) {
          rt.append(el("tr", { className: "static-row" },
            el("th", { scope: "row", className: "mono" }, c.host),
            el("td", { className: "mono" }, c.username),
            el("td", { className: "mono" }, c.insecure ? "HTTP" : "HTTPS"),
            el("td", null, el("button", {
              type: "button",
              className: "btn btn-sm btn-danger",
              onclick: async () => {
                await api(`/v1/credentials/registry/${encodeURIComponent(c.host)}`, { method: "DELETE" });
                refreshAll();
              },
            }, "删除"))
          ));
        }
      }
    }

    // Drawer + NDJSON live log streaming
    const drawer = document.getElementById("detail-drawer");
    drawer.addEventListener("close", () => {
      state.selectedBuildId = "";
      if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
    });

    async function openBuildDrawer(buildId) {
      state.selectedBuildId = buildId;
      if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
      const logBox = document.getElementById("drawer-logs");
      logBox.replaceChildren();
      if (!drawer.open) drawer.showModal();

      const data = await api(`/v1/builds/${encodeURIComponent(buildId)}`);
      const b = data.build;
      renderDrawerHeader(b);
      for (const ev of b.events || []) appendLogLine(logBox, ev);

      const terminal = ["succeeded", "failed", "canceled"].includes(b.status);
      if (terminal) {
        document.getElementById("drawer-stream-state").textContent = "已结束";
        return;
      }

      // Stream live NDJSON updates while running
      document.getElementById("drawer-stream-state").textContent = "● 实时日志流连接中…";
      const ctrl = new AbortController();
      state.streamAbort = ctrl;
      let seenSeq = (b.events || []).reduce((m, e) => Math.max(m, e.seq || 0), 0);
      try {
        const res = await fetch(`/v1/builds/${encodeURIComponent(buildId)}/stream`, {
          headers: { "Authorization": `Bearer ${state.token}` },
          signal: ctrl.signal,
        });
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n")) >= 0) {
            const raw = buf.slice(0, idx).trim();
            buf = buf.slice(idx + 1);
            if (!raw) continue;
            const msg = JSON.parse(raw);
            if (msg.type === "event" && (msg.seq || 0) > seenSeq) {
              seenSeq = msg.seq;
              appendLogLine(logBox, msg);
            } else if (msg.type === "done" && msg.build) {
              renderDrawerHeader(msg.build);
              document.getElementById("drawer-stream-state").textContent = "已结束";
              refreshAll();
            }
          }
        }
      } catch (err) {
        if (err.name !== "AbortError") {
          document.getElementById("drawer-stream-state").textContent = "连接中断";
        }
      }
    }

    function renderDrawerHeader(b) {
      document.getElementById("drawer-title").textContent = `${b.repoSlug} @ ${b.ref}`;
      document.getElementById("drawer-subtitle").textContent = `${b.id} · ${b.status}`;
      const cancelBtn = document.getElementById("drawer-cancel-btn");
      cancelBtn.hidden = ["succeeded", "failed", "canceled"].includes(b.status);
      cancelBtn.onclick = async () => {
        await api(`/v1/builds/${encodeURIComponent(b.id)}/cancel`, { method: "POST", body: {} });
        openBuildDrawer(b.id);
        refreshAll();
      };
      document.getElementById("drawer-retry-btn").onclick = async () => {
        const res = await api(`/v1/builds/${encodeURIComponent(b.id)}/retry`, { method: "POST", body: {} });
        refreshAll();
        openBuildDrawer(res.build.id);
      };
      const kv = document.getElementById("drawer-kv");
      kv.replaceChildren();
      const fields = [
        ["当前状态", renderStatusPill(b.status)],
        ["执行 Runner", b.runner || b.pinnedRunner || "等待调度"],
        ["目标平台", (b.platforms || []).join(", ")],
        ["源码提交", (b.result && b.result.commit) ? b.result.commit.slice(0, 12) : b.ref],
        ["产物镜像", (b.result && b.result.image) || "-"],
        ["创建时间", fmtTime(b.createdAt)],
      ];
      for (const [k, v] of fields) {
        kv.append(el("div", { className: "kv-item" }, el("dt", null, k), el("dd", { className: "mono" }, v)));
      }
    }

    function appendLogLine(container, ev) {
      const cls = ev.kind === "error" ? "log-line log-err" : "log-line";
      container.append(
        el("div", { className: cls },
          el("span", { className: "log-stage" }, `[${ev.stage || "build"}]`),
          ev.message || ""
        )
      );
      container.scrollTop = container.scrollHeight;
    }

    // Filters & Buttons
    document.getElementById("refresh-builds-btn").addEventListener("click", refreshAll);
    document.getElementById("refresh-runners-btn").addEventListener("click", refreshAll);
    document.getElementById("filter-repo").addEventListener("input", (ev) => {
      state.repoFilter = ev.target.value.trim();
      refreshAll();
    });
    for (const seg of document.querySelectorAll("#status-segments .seg-btn")) {
      seg.addEventListener("click", () => {
        state.statusFilter = seg.getAttribute("data-status") || "";
        for (const s of document.querySelectorAll("#status-segments .seg-btn")) {
          s.setAttribute("aria-pressed", s === seg ? "true" : "false");
        }
        refreshAll();
      });
    }

    // Submit Build modal
    const submitDialog = document.getElementById("submit-dialog");
    document.getElementById("open-submit-btn").addEventListener("click", () => {
      document.getElementById("submit-error").hidden = true;
      submitDialog.showModal();
    });
    document.getElementById("submit-build-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("submit-error");
      errEl.hidden = true;
      const src = document.getElementById("sub-source").value.trim();
      const isUrl = src.includes("://") || src.startsWith("git@");
      const manifestPath = document.getElementById("sub-manifest-path").value.trim();
      const composeSidecar = document.getElementById("sub-compose-sidecar").value.trim();
      if (manifestPath && composeSidecar) {
        errEl.textContent = "单服务清单路径与 Compose 清单路径不能同时填写";
        errEl.hidden = false;
        return;
      }
      const body = {
        repoUrl: isUrl ? src : "",
        repository: isUrl ? "" : src,
        ref: document.getElementById("sub-ref").value.trim() || "main",
        providerId: document.getElementById("sub-provider-id").value.trim(),
        platform: document.getElementById("sub-platform").value,
        runner: document.getElementById("sub-runner").value.trim(),
        context: document.getElementById("sub-context").value.trim(),
        dockerfile: document.getElementById("sub-dockerfile").value.trim(),
        manifestPath,
        composeSidecar,
        region: document.getElementById("sub-region").value.trim(),
        node: document.getElementById("sub-node").value.trim(),
        deploy: !document.getElementById("sub-no-deploy").checked,
        noCache: document.getElementById("sub-no-cache").checked,
      };
      try {
        const res = await api("/v1/builds", { method: "POST", body });
        submitDialog.close();
        await refreshAll();
        openBuildDrawer(res.build.id);
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
      }
    });

    // Runner Token modal
    const tokenDialog = document.getElementById("token-dialog");
    document.getElementById("open-token-btn").addEventListener("click", () => {
      document.getElementById("token-error").hidden = true;
      document.getElementById("token-result-box").hidden = true;
      tokenDialog.showModal();
    });
    document.getElementById("create-token-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("token-error");
      errEl.hidden = true;
      try {
        const res = await api("/v1/runners/tokens", {
          method: "POST",
          body: {
            name: document.getElementById("tok-name").value.trim(),
            weight: Number(document.getElementById("tok-weight").value || 100),
            maxConcurrency: Number(document.getElementById("tok-concurrency").value || 1),
            cacheMode: document.getElementById("tok-cache").value,
            rotate: document.getElementById("tok-rotate").checked,
          },
        });
        const r = res.runner;
        const origin = window.location.origin;
        document.getElementById("token-cli-snippet").textContent =
          `# 方式 A：加入并默认启动后台服务（macOS / Linux，关闭终端后继续运行）\n` +
          `luma runner join --url "${origin}" --token "${r.runnerToken}" --cache-mode ${r.cacheMode}\n` +
          `# 日常管理：luma runner start / stop / restart / status / logs --follow`;
        document.getElementById("token-docker-snippet").textContent =
          `# 方式 B：Linux 容器一键启动\n` +
          `docker run -d --name luma-runner-${r.name} --restart=on-failure --network=host \\\n` +
          `  -v /var/run/docker.sock:/var/run/docker.sock -v luma-runner-state:/var/lib/luma-runner \\\n` +
          `  -e LUMA_BUILD_URL="${origin}" -e LUMA_RUNNER_TOKEN="${r.runnerToken}" \\\n` +
          `  luma-build-runner:latest`;
        document.getElementById("token-result-box").hidden = false;
        refreshAll();
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
      }
    });

    // Dynamic Git & Registry credential modals
    const gcDialog = document.getElementById("git-cred-dialog");
    document.getElementById("open-git-cred-btn").addEventListener("click", () => {
      document.getElementById("git-cred-error").hidden = true;
      gcDialog.showModal();
    });
    document.getElementById("git-cred-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("git-cred-error");
      errEl.hidden = true;
      try {
        await api("/v1/credentials/git", {
          method: "POST",
          body: {
            id: document.getElementById("gc-id").value.trim(),
            host: document.getElementById("gc-host").value.trim(),
            username: document.getElementById("gc-user").value.trim(),
            token: document.getElementById("gc-token").value.trim(),
          },
        });
        document.getElementById("gc-token").value = "";
        gcDialog.close();
        refreshAll();
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
      }
    });

    const rcDialog = document.getElementById("reg-cred-dialog");
    document.getElementById("open-reg-cred-btn").addEventListener("click", () => {
      document.getElementById("reg-cred-error").hidden = true;
      rcDialog.showModal();
    });
    document.getElementById("reg-cred-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("reg-cred-error");
      errEl.hidden = true;
      try {
        await api("/v1/credentials/registry", {
          method: "POST",
          body: {
            host: document.getElementById("rc-host").value.trim(),
            username: document.getElementById("rc-user").value.trim(),
            password: document.getElementById("rc-pass").value.trim(),
          },
        });
        document.getElementById("rc-pass").value = "";
        rcDialog.close();
        refreshAll();
      } catch (err) {
        errEl.textContent = err.message;
        errEl.hidden = false;
      }
    });

    // Boot
    const existing = loadStoredToken();
    if (existing) {
      state.token = existing;
      api("/v1/status").then(() => enterShell()).catch(() => signOut());
    } else {
      document.getElementById("auth-view").hidden = false;
    }
  })();
  </script>
</body>
</html>
"""
