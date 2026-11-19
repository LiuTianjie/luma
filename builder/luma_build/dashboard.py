"""Luma Kumo-styled Web Console for the decoupled luma-build service.

Follows Luma Control and Luma Eye's shared visual design system (official Luma
logo mark, Kumo OKLCH semantic tokens, Phosphor icons, tabular numerals,
quiet-dot / soft-pill StatePill badges, right-hand DetailDrawer, and
system-aware light/dark theming) with zero external runtime asset dependencies.
"""
from __future__ import annotations

import base64
from pathlib import Path
import secrets
from typing import Tuple

_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "luma-logo-mark.png"
FAVICON_PNG: bytes = _LOGO_PATH.read_bytes()
_LOGO_B64 = base64.b64encode(FAVICON_PNG).decode("ascii")
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">'
    f'<image href="data:image/png;base64,{_LOGO_B64}" width="96" height="96"/>'
    "</svg>\n"
)


def render_dashboard_html(
    *,
    version: str,
    control_sso: bool = False,
    session_state: str = "none",
) -> Tuple[str, str]:
    """Return ``(html_body, csp_nonce)`` with a fresh per-request nonce."""
    nonce = secrets.token_urlsafe(18)
    valid_state = (
        session_state
        if session_state in {"none", "authenticated", "check", "expired", "unavailable", "manual"}
        else "none"
    )
    authenticated = valid_state == "authenticated"
    html = (
        _TEMPLATE.replace("__CSP_NONCE__", nonce)
        .replace("__LUMA_BUILD_VERSION__", version)
        .replace("__LUMA_CONTROL_SSO__", "true" if control_sso else "false")
        .replace("__LUMA_SESSION_STATE__", f'"{valid_state}"')
        .replace("__AUTH_VIEW_HIDDEN__", " hidden")
        .replace("__AUTH_CHECKING_HIDDEN__", " hidden" if authenticated else "")
        .replace("__APP_SHELL_HIDDEN__", "" if authenticated else " hidden")
    )
    return html, nonce


_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f5f3f3">
  <meta name="color-scheme" content="light dark">
  <script nonce="__CSP_NONCE__">
    try {
      const savedScheme = localStorage.getItem("luma-build-color-scheme");
      const schemeMeta = document.querySelector('meta[name="color-scheme"]');
      if (savedScheme === "light" || savedScheme === "dark") {
        schemeMeta.content = savedScheme;
      }
      const isDark = savedScheme === "dark" || (savedScheme !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
      const themeMeta = document.querySelector('meta[name="theme-color"]');
      if (themeMeta) themeMeta.content = isDark ? "#0a0a0a" : "#f5f3f3";
    } catch {}
  </script>
  <title>Luma Build · 控制台</title>
  <link rel="icon" type="image/png" href="/favicon.png">
  <style nonce="__CSP_NONCE__">
    :root {
      color-scheme: light dark;
      --font-sans: "Geist Variable", -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

      /* Shared Kumo OKLCH semantic tokens aligned 1:1 with Luma Control & Luma Eye */
      --canvas-light: oklch(0.973 0 0);
      --canvas-dark: oklch(0.145 0 0);
      --bg-light: oklch(1 0 0);
      --bg-dark: oklch(0.18 0 0);
      --fg-light: oklch(0.21 0.006 285.9);
      --fg-dark: oklch(0.97 0 0);
      --card-light: oklch(1 0 0);
      --card-dark: oklch(0.195 0 0);
      --popover-light: oklch(1 0 0);
      --popover-dark: oklch(0.225 0 0);
      --secondary-light: oklch(0.96 0 0);
      --secondary-dark: oklch(0.25 0 0);
      --muted-light: oklch(0.97 0 0);
      --muted-dark: oklch(0.22 0 0);
      --muted-fg-light: oklch(0.48 0 0);
      --muted-fg-dark: oklch(0.72 0 0);
      --subtle-fg-light: oklch(0.556 0 0);
      --subtle-fg-dark: oklch(0.66 0 0);
      --accent-light: oklch(0.955 0 0);
      --accent-dark: oklch(0.255 0 0);
      --seg-active-light: oklch(1 0 0);
      --seg-active-dark: oklch(0.255 0 0);
      --primary-light: oklch(0.5772 0.2324 260);
      --primary-dark: oklch(0.55 0.2 260);
      --primary-hover-light: oklch(0.488 0.243 264.4);
      --primary-hover-dark: oklch(0.6 0.19 258);
      --link-light: oklch(0.488 0.243 264.4);
      --link-dark: oklch(0.74 0.15 254.6);
      --border-light: oklch(0.145 0 0 / 0.1);
      --border-dark: oklch(1 0 0 / 0.1);
      --hairline-light: oklch(0.935 0 0);
      --hairline-dark: oklch(1 0 0 / 0.07);
      --input-light: oklch(0.145 0 0 / 0.14);
      --input-dark: oklch(1 0 0 / 0.14);
      --shadow-light: oklch(0 0 0 / 0.06);
      --shadow-dark: oklch(0 0 0 / 0.35);
      --scrollbar-thumb-light: oklch(0.66 0 0);
      --scrollbar-thumb-dark: oklch(0.42 0 0);

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

      --info-light: oklch(0.623 0.214 259.8);
      --info-dark: oklch(0.66 0.17 255);
      --info-fg-light: oklch(0.424 0.199 265.6);
      --info-fg-dark: oklch(0.8 0.11 254.6);
      --info-soft-light: oklch(0.966 0.018 255.6);
      --info-soft-dark: oklch(0.26 0.06 262);

      --danger-light: oklch(0.637 0.237 25.3);
      --danger-dark: oklch(0.65 0.21 24);
      --danger-fill-light: oklch(0.54 0.22 26);
      --danger-fill-dark: oklch(0.54 0.2 25);
      --danger-fg-light: oklch(0.505 0.213 27.5);
      --danger-fg-dark: oklch(0.76 0.15 22.2);
      --danger-soft-light: oklch(0.965 0.018 17.7);
      --danger-soft-dark: oklch(0.27 0.06 25);

      --canvas: var(--canvas-light);
      --background: var(--bg-light);
      --foreground: var(--fg-light);
      --card: var(--card-light);
      --popover: var(--popover-light);
      --secondary: var(--secondary-light);
      --muted: var(--muted-light);
      --muted-foreground: var(--muted-fg-light);
      --subtle-foreground: var(--subtle-fg-light);
      --accent: var(--accent-light);
      --seg-active: var(--seg-active-light);
      --primary: var(--primary-light);
      --primary-hover: var(--primary-hover-light);
      --link: var(--link-light);
      --border: var(--border-light);
      --hairline: var(--hairline-light);
      --input: var(--input-light);
      --shadow-color: var(--shadow-light);
      --scrollbar-thumb: var(--scrollbar-thumb-light);
      --success: var(--success-light);
      --success-foreground: var(--success-fg-light);
      --success-soft: var(--success-soft-light);
      --warning: var(--warning-light);
      --warning-foreground: var(--warning-fg-light);
      --warning-soft: var(--warning-soft-light);
      --info: var(--info-light);
      --info-foreground: var(--info-fg-light);
      --info-soft: var(--info-soft-light);
      --danger: var(--danger-light);
      --danger-fill: var(--danger-fill-light);
      --danger-foreground: var(--danger-fg-light);
      --danger-soft: var(--danger-soft-light);

      --radius: 0.5rem;
      --card-spacing: 1.25rem;
      --header-height: 3.5rem;
      --shadow-xs: 0 1px 2px 0 var(--shadow-color);
      --shadow-sm: 0 1px 3px 0 var(--shadow-color), 0 1px 2px -1px var(--shadow-color);
      accent-color: var(--primary);
      scrollbar-color: var(--scrollbar-thumb) transparent;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --canvas: var(--canvas-dark);
        --background: var(--bg-dark);
        --foreground: var(--fg-dark);
        --card: var(--card-dark);
        --popover: var(--popover-dark);
        --secondary: var(--secondary-dark);
        --muted: var(--muted-dark);
        --muted-foreground: var(--muted-fg-dark);
        --subtle-foreground: var(--subtle-fg-dark);
        --accent: var(--accent-dark);
        --seg-active: var(--seg-active-dark);
        --primary: var(--primary-dark);
        --primary-hover: var(--primary-hover-dark);
        --link: var(--link-dark);
        --border: var(--border-dark);
        --hairline: var(--hairline-dark);
        --input: var(--input-dark);
        --shadow-color: var(--shadow-dark);
        --scrollbar-thumb: var(--scrollbar-thumb-dark);
        --success: var(--success-dark);
        --success-foreground: var(--success-fg-dark);
        --success-soft: var(--success-soft-dark);
        --warning: var(--warning-dark);
        --warning-foreground: var(--warning-fg-dark);
        --warning-soft: var(--warning-soft-dark);
        --info: var(--info-dark);
        --info-foreground: var(--info-fg-dark);
        --info-soft: var(--info-soft-dark);
        --danger: var(--danger-dark);
        --danger-fill: var(--danger-fill-dark);
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
        --popover: light-dark(var(--popover-light), var(--popover-dark));
        --secondary: light-dark(var(--secondary-light), var(--secondary-dark));
        --muted: light-dark(var(--muted-light), var(--muted-dark));
        --muted-foreground: light-dark(var(--muted-fg-light), var(--muted-fg-dark));
        --subtle-foreground: light-dark(var(--subtle-fg-light), var(--subtle-fg-dark));
        --accent: light-dark(var(--accent-light), var(--accent-dark));
        --seg-active: light-dark(var(--seg-active-light), var(--seg-active-dark));
        --primary: light-dark(var(--primary-light), var(--primary-dark));
        --primary-hover: light-dark(var(--primary-hover-light), var(--primary-hover-dark));
        --link: light-dark(var(--link-light), var(--link-dark));
        --border: light-dark(var(--border-light), var(--border-dark));
        --hairline: light-dark(var(--hairline-light), var(--hairline-dark));
        --input: light-dark(var(--input-light), var(--input-dark));
        --shadow-color: light-dark(var(--shadow-light), var(--shadow-dark));
        --scrollbar-thumb: light-dark(var(--scrollbar-thumb-light), var(--scrollbar-thumb-dark));
        --success: light-dark(var(--success-light), var(--success-dark));
        --success-foreground: light-dark(var(--success-fg-light), var(--success-fg-dark));
        --success-soft: light-dark(var(--success-soft-light), var(--success-soft-dark));
        --warning: light-dark(var(--warning-light), var(--warning-dark));
        --warning-foreground: light-dark(var(--warning-fg-light), var(--warning-fg-dark));
        --warning-soft: light-dark(var(--warning-soft-light), var(--warning-soft-dark));
        --info: light-dark(var(--info-light), var(--info-dark));
        --info-foreground: light-dark(var(--info-fg-light), var(--info-fg-dark));
        --info-soft: light-dark(var(--info-soft-light), var(--info-soft-dark));
        --danger: light-dark(var(--danger-light), var(--danger-dark));
        --danger-fill: light-dark(var(--danger-fill-light), var(--danger-fill-dark));
        --danger-foreground: light-dark(var(--danger-fg-light), var(--danger-fg-dark));
        --danger-soft: light-dark(var(--danger-soft-light), var(--danger-soft-dark));
      }
    }

    /* CSS-only theme toggle icon branching (no JS needed when OS scheme changes) */
    .theme-opt-light { display: none; }
    .theme-opt-dark { display: inline-flex; align-items: center; }
    @media (prefers-color-scheme: dark) {
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .theme-opt-light {
        display: inline-flex;
        align-items: center;
      }
      :root:not(:has(> head > meta[name="color-scheme"][content="light"])) .theme-opt-dark {
        display: none;
      }
    }
    :root:has(> head > meta[name="color-scheme"][content="dark"]) .theme-opt-light {
      display: inline-flex;
      align-items: center;
    }
    :root:has(> head > meta[name="color-scheme"][content="dark"]) .theme-opt-dark {
      display: none;
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
    ::selection { background: color-mix(in oklch, var(--primary) 22%, transparent); }
    table, [data-numeric] { font-variant-numeric: tabular-nums; }
    code, pre, .mono { font-family: var(--font-mono); }

    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
      border: 0;
    }

    .icon {
      width: 1rem;
      height: 1rem;
      flex-shrink: 0;
      display: inline-block;
      pointer-events: none;
    }
    .icon-sm {
      width: 0.875rem;
      height: 0.875rem;
    }

    /* Shell layout (aligned with Luma Control Sidebar + Topbar + Console Content) */
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
      transition: width 0.15s ease;
    }
    .shell.collapsed .sidebar { width: 3.5rem; }
    .shell.collapsed .hide-collapsed { display: none !important; }
    .shell.collapsed .sidebar-header { justify-content: center; padding-inline: 0.5rem; }
    .shell.collapsed .sidebar-body { padding-inline: 0.5rem; }
    .shell.collapsed .sidebar-footer { padding: 0.5rem; }
    .shell.collapsed .nav-btn { justify-content: center; padding-inline: 0; width: 2rem; margin-inline: auto; }

    .sidebar-header {
      height: var(--header-height);
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding-inline: 0.75rem;
      border-block-end: 1px solid var(--border);
    }
    .brand-link {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      min-width: 0;
      padding: 0.25rem;
      border-radius: 0.375rem;
      color: inherit;
      text-decoration: none;
    }
    .brand-mark {
      width: 2rem;
      height: 2rem;
      border-radius: var(--radius);
      background: var(--background);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
    }
    .brand-logo {
      width: 1.25rem;
      height: 1.25rem;
      display: block;
    }
    .brand-meta { display: flex; flex-direction: column; min-width: 0; gap: 0.2rem; line-height: 1; }
    .brand-meta strong { font-size: 0.9375rem; font-weight: 600; letter-spacing: -0.01em; color: var(--foreground); }
    .brand-meta span { font-size: 0.6875rem; color: var(--muted-foreground); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .sidebar-body {
      flex: 1;
      padding: 0.75rem 0.625rem 0.5rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      overflow-y: auto;
    }
    .nav-group { padding-block: 0.25rem; }
    .nav-group-label {
      height: 1.75rem;
      display: flex;
      align-items: center;
      padding-inline: 0.625rem;
      font-size: 0.6875rem;
      font-weight: 500;
      color: var(--subtle-foreground);
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .nav-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; }
    .nav-btn {
      width: 100%;
      height: 2rem;
      border: 0;
      background: transparent;
      color: var(--muted-foreground);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding-inline: 0.625rem;
      border-radius: 0.375rem;
      font-size: 0.8125rem;
      font-weight: 400;
      cursor: pointer;
      text-align: start;
      transition: background-color 0.12s ease, color 0.12s ease;
    }
    .nav-btn:hover { background: var(--accent); color: var(--foreground); }
    .nav-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
    .nav-btn[aria-current="page"] {
      background: var(--accent);
      color: var(--foreground);
      font-weight: 500;
    }
    .nav-badge {
      margin-inline-start: auto;
      min-width: 1.25rem;
      height: 1.25rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.6875rem;
      font-weight: 500;
      padding-inline: 0.375rem;
      border-radius: 999px;
      background: var(--muted);
      color: var(--muted-foreground);
      font-variant-numeric: tabular-nums;
    }
    .nav-badge.is-active {
      background: var(--warning-soft);
      color: var(--warning-foreground);
    }

    .sidebar-footer {
      padding: 0.625rem;
      border-block-start: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 0.125rem;
    }
    .fleet-summary {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      height: 2rem;
      padding-inline: 0.625rem;
      border-radius: 0.375rem;
      font-size: 0.75rem;
      color: var(--muted-foreground);
      border: 0;
      background: transparent;
      cursor: pointer;
      text-align: start;
      width: 100%;
      transition: background-color 0.12s ease, color 0.12s ease;
    }
    .fleet-summary:hover { background: var(--accent); color: var(--foreground); }
    .fleet-dot {
      width: 0.375rem;
      height: 0.375rem;
      border-radius: 999px;
      background: var(--subtle-foreground);
      flex-shrink: 0;
    }
    .fleet-dot.tone-ok { background: var(--success); }
    .fleet-dot.tone-warn { background: var(--warning); }
    .fleet-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .main-area { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    .topbar {
      height: var(--header-height);
      flex-shrink: 0;
      background: var(--background);
      border-block-end: 1px solid var(--border);
      padding-inline: 1rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      position: sticky;
      inset-block-start: 0;
      z-index: 5;
    }
    @media (min-width: 768px) {
      .topbar { padding-inline: 1.5rem; }
    }
    .breadcrumb {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      min-width: 0;
      font-size: 0.8125rem;
    }
    .breadcrumb-root { color: var(--muted-foreground); white-space: nowrap; }
    .breadcrumb-sep { color: var(--subtle-foreground); user-select: none; }
    .breadcrumb-current { color: var(--foreground); font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .topbar-actions { display: flex; align-items: center; gap: 0.375rem; margin-inline-start: auto; }
    .sync-status {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      margin-inline-end: 0.375rem;
      font-size: 0.75rem;
      color: var(--muted-foreground);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .sync-dot {
      width: 0.375rem;
      height: 0.375rem;
      border-radius: 999px;
      background: var(--success);
      flex-shrink: 0;
    }
    .sync-dot.is-err { background: var(--danger); }

    .console-content {
      flex: 1;
      min-width: 0;
      min-height: 0;
      overflow: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      background: var(--canvas);
      padding: 1.75rem clamp(1.25rem, 3vw, 2.5rem) 3rem;
    }
    .console-page {
      max-width: 1400px;
      width: 100%;
      min-width: 0;
      margin-inline: auto;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }

    /* Page header (Kumo ResourceListPage) */
    .page-header {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem 1.5rem;
    }
    .page-header-copy {
      display: flex;
      flex-direction: column;
      gap: 0.375rem;
      min-width: 0;
      flex: 1 1 18rem;
    }
    .page-header h1 {
      margin: 0;
      font-size: 1.625rem;
      line-height: 2rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
    .page-header p {
      margin: 0;
      max-width: 48rem;
      color: var(--muted-foreground);
      font-size: 0.875rem;
      line-height: 1.5rem;
    }
    .header-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      padding-block-start: 0.125rem;
    }

    /* Buttons & controls (Kumo emphasis treatment) */
    button, input, select { font: inherit; }
    .btn {
      position: relative;
      display: inline-flex;
      flex-shrink: 0;
      align-items: center;
      justify-content: center;
      gap: 0.375rem;
      height: 2rem;
      padding-inline: 0.75rem;
      border-radius: var(--radius);
      font-size: 0.8125rem;
      font-weight: 500;
      cursor: pointer;
      border: 1px solid transparent;
      background: var(--background);
      color: var(--foreground);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
      white-space: nowrap;
      text-decoration: none;
      user-select: none;
      transition: color 0.12s ease, background-color 0.12s ease, box-shadow 0.12s ease;
    }
    .btn:hover { background: var(--accent); }
    .btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
    .btn:disabled { opacity: 0.5; pointer-events: none; }

    .btn-primary {
      background-color: var(--primary);
      background-image: linear-gradient(to bottom, var(--primary), color-mix(in oklch, var(--primary), black 9%));
      border-color: transparent;
      color: #fff;
      box-shadow: inset 0 1px 0 0 rgb(255 255 255 / 0.16), 0 0 0 1px color-mix(in oklch, var(--primary), black 14%), var(--shadow-xs);
    }
    .btn-primary:hover {
      background-color: var(--primary-hover);
      background-image: linear-gradient(to bottom, color-mix(in oklch, var(--primary), black 6%), color-mix(in oklch, var(--primary), black 15%));
    }
    .btn-primary:active {
      background-image: linear-gradient(to bottom, color-mix(in oklch, var(--primary), black 12%), color-mix(in oklch, var(--primary), black 12%));
    }

    .btn-danger {
      background: var(--background);
      color: var(--danger-foreground);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
    }
    .btn-danger:hover {
      background: var(--danger-soft);
      box-shadow: 0 0 0 1px color-mix(in oklch, var(--danger) 30%, transparent), var(--shadow-xs);
    }
    .btn-danger:focus-visible { outline-color: var(--danger); }

    .btn-ghost {
      background: transparent;
      border-color: transparent;
      box-shadow: none;
      color: var(--foreground);
    }
    .btn-ghost:hover { background: var(--accent); }

    .btn-sm {
      height: 1.75rem;
      padding-inline: 0.625rem;
      border-radius: 0.375rem;
      font-size: 0.8125rem;
      gap: 0.25rem;
    }
    .btn-icon {
      width: 1.75rem;
      height: 1.75rem;
      padding: 0;
      border-radius: 0.375rem;
    }

    .input, .select {
      height: 2rem;
      padding-inline: 0.625rem;
      border-radius: var(--radius);
      border: 1px solid var(--input);
      background: var(--background);
      color: var(--foreground);
      font-size: 0.8125rem;
      outline: none;
      box-shadow: var(--shadow-xs);
      transition: border-color 0.12s ease, box-shadow 0.12s ease;
    }
    .input::placeholder { color: var(--muted-foreground); }
    .input:focus, .select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 20%, transparent);
    }

    /* InputGroup for search */
    .search-group {
      position: relative;
      display: flex;
      align-items: center;
      flex: 1 1 14rem;
      max-width: 20rem;
    }
    .search-group .search-icon {
      position: absolute;
      inset-inline-start: 0.625rem;
      color: var(--muted-foreground);
      pointer-events: none;
    }
    .search-group .input {
      width: 100%;
      padding-inline-start: 2rem;
    }

    /* Stat tiles (aligned with Luma StatTile) */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
      gap: 0.75rem;
    }
    .stat-tile {
      background: var(--card);
      border-radius: var(--radius);
      padding: 0.875rem 1rem;
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      min-width: 0;
    }
    .stat-label { font-size: 0.75rem; font-weight: 500; color: var(--muted-foreground); line-height: 1.2; }
    .stat-value { font-size: 1.25rem; line-height: 1.1; font-weight: 600; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
    .stat-sub { font-size: 0.75rem; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* Filter toolbar + Kumo segmented ToggleGroup */
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
    }
    .segmented {
      display: inline-flex;
      align-items: center;
      height: 2rem;
      max-width: 100%;
      overflow-x: auto;
      padding: 3px;
      border-radius: var(--radius);
      background: var(--secondary);
      box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--border) 70%, transparent);
      gap: 2px;
    }
    .seg-btn {
      height: 100%;
      border: 0;
      background: transparent;
      color: var(--muted-foreground);
      font-size: 0.8125rem;
      font-weight: 500;
      padding-inline: 0.625rem;
      border-radius: 0.375rem;
      cursor: pointer;
      white-space: nowrap;
      transition: color 0.12s ease, background-color 0.12s ease, box-shadow 0.12s ease;
    }
    .seg-btn:hover { color: var(--foreground); }
    .seg-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
    .seg-btn[aria-pressed="true"] {
      background: var(--seg-active);
      color: var(--foreground);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
    }

    /* Kumo LayerCard & Edge-to-Edge Responsive Table */
    .card {
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
      overflow: hidden;
    }
    .card-header {
      padding: 1rem var(--card-spacing);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      border-block-end: 1px solid var(--hairline);
    }
    .card-title {
      font-size: 0.9375rem;
      font-weight: 600;
      letter-spacing: -0.005em;
      line-height: 1.35;
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
      font-size: 0.8125rem;
    }
    thead {
      position: sticky;
      inset-block-start: 0;
      z-index: 1;
      background: color-mix(in oklch, var(--muted) 70%, var(--card));
    }
    thead th {
      height: 2.25rem;
      padding: 0 0.75rem;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--muted-foreground);
      border-block-end: 1px solid var(--hairline);
      white-space: nowrap;
      text-align: start;
    }
    thead th:first-child, tbody th:first-child, tbody td:first-child { padding-inline-start: var(--card-spacing); }
    thead th:last-child, tbody th:last-child, tbody td:last-child { padding-inline-end: var(--card-spacing); }

    tbody tr { cursor: pointer; transition: background-color 0.12s ease; }
    tbody tr.static-row { cursor: default; }
    tbody tr:hover { background: color-mix(in oklch, var(--muted) 45%, transparent); }
    tbody th, tbody td {
      padding: 0.625rem 0.75rem;
      border-block-end: 1px solid var(--hairline);
      vertical-align: middle;
      font-weight: 400;
    }
    tbody tr:last-child th, tbody tr:last-child td { border-block-end: 0; }

    /* Keep long build summaries from pushing the remaining columns off-screen. */
    .resp-table { table-layout: fixed; }
    .resp-table thead th:nth-child(2) { width: 6.5rem; }
    .resp-table thead th:nth-child(3) { width: 13.5rem; }
    .resp-table thead th:nth-child(4) { width: 11.5rem; text-align: end; }
    .resp-table tbody td[data-col="time"] { text-align: end; }
    .resp-table .cell-meta { overflow-wrap: anywhere; }

    .record-primary { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
    .record-title { color: var(--link); font-weight: 500; font-size: 0.8125rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    tbody tr:hover .record-title { text-decoration: underline; text-underline-offset: 3px; }
    .record-sub { color: var(--muted-foreground); font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    @container (width < 640px) {
      .resp-table, .resp-table thead, .resp-table tbody, .resp-table tr, .resp-table th, .resp-table td {
        display: block;
      }
      .resp-table thead { display: none; }
      .resp-table tbody tr {
        padding: 0.5rem 0;
        border-block-end: 1px solid var(--hairline);
      }
      .resp-table tbody th, .resp-table tbody td {
        border-block-end: 0;
        padding: 0.3rem var(--card-spacing);
        text-align: start;
      }
      .resp-table tbody td[data-col="time"] { text-align: start; }
      .resp-table tbody td[data-col="status"]::before { content: "状态："; color: var(--muted-foreground); font-size: 0.75rem; }
      .resp-table tbody td[data-col="meta"]::before { content: "架构 / 产物："; color: var(--muted-foreground); font-size: 0.75rem; }
      .resp-table tbody td[data-col="time"]::before { content: "创建时间："; color: var(--muted-foreground); font-size: 0.75rem; }
    }

    /* Kumo StatePill badges (aligned with ui/badge.tsx + primitives.tsx) */
    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.375rem;
      height: 1.25rem;
      font-size: 0.75rem;
      font-weight: 500;
      padding-inline: 0.5rem;
      border-radius: 999px;
      line-height: 1;
      white-space: nowrap;
      border: 1px solid transparent;
    }
    .pill-dot {
      padding-inline-start: 0.375rem;
    }
    .pill-dot::before {
      content: "";
      width: 0.375rem;
      height: 0.375rem;
      border-radius: 999px;
      background: currentColor;
      flex-shrink: 0;
    }
    .pill-success {
      background: var(--background);
      border-color: var(--border);
      color: var(--foreground);
    }
    .pill-success::before { background: var(--success); }
    .pill-running {
      background: var(--info-soft);
      color: var(--info-foreground);
    }
    .pill-running::before { background: var(--info); }
    .pill-warning {
      background: var(--warning-soft);
      color: var(--warning-foreground);
    }
    .pill-danger {
      background: var(--danger-soft);
      color: var(--danger-foreground);
    }
    .pill-neutral {
      background: var(--background);
      border-color: var(--border);
      color: var(--foreground);
    }
    .pill-neutral::before { background: var(--subtle-foreground); }

    .empty-state {
      padding: 3rem 1.5rem;
      text-align: center;
      color: var(--muted-foreground);
      font-size: 0.8125rem;
    }
    .empty-state strong { display: block; color: var(--foreground); font-size: 0.9375rem; font-weight: 600; margin-block-end: 0.25rem; }

    /* Native <dialog> modals & right-side detail sheet */
    dialog {
      border: 0;
      background: var(--card);
      color: var(--foreground);
      padding: 0;
      box-shadow: 0 0 0 1px var(--border), 0 12px 36px var(--shadow-color);
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
      box-shadow: -1px 0 0 0 var(--border), 0 12px 36px var(--shadow-color);
    }
    dialog.drawer-sheet[open] {
      display: flex;
      flex-direction: column;
    }
    .dialog-header {
      padding: 1rem 1.25rem;
      border-block-end: 1px solid var(--hairline);
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
    }
    .dialog-header h2 { margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
    .dialog-header p { margin: 0.2rem 0 0; font-size: 0.75rem; color: var(--muted-foreground); }
    .dialog-body { padding: 1.25rem; display: flex; flex-direction: column; gap: 0.875rem; overflow-y: auto; }
    .drawer-body { flex: 1; min-height: 0; padding: 1rem 1.25rem; display: flex; flex-direction: column; gap: 1rem; overflow-y: auto; }
    .dialog-footer {
      padding: 0.875rem 1.25rem;
      border-block-start: 1px solid var(--hairline);
      background: color-mix(in oklch, var(--muted) 40%, transparent);
      display: flex;
      justify-content: flex-end;
      gap: 0.5rem;
    }
    .field { display: flex; flex-direction: column; gap: 0.375rem; }
    .field label { font-size: 0.75rem; font-weight: 500; color: var(--foreground); }
    .field small { font-size: 0.75rem; color: var(--muted-foreground); }
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
    .check-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8125rem; cursor: pointer; }
    .adv-box {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--muted);
      padding: 0.625rem 0.875rem;
    }
    .adv-summary {
      cursor: pointer;
      font-size: 0.8125rem;
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
      gap: 0.875rem;
      padding: 1rem 1.25rem;
      border-radius: var(--radius);
      background: var(--card);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
    }
    .kv-grid-muted {
      background: color-mix(in oklch, var(--muted) 65%, var(--card));
      box-shadow: 0 0 0 1px var(--hairline);
    }
    .kv-item dt { font-size: 0.75rem; color: var(--muted-foreground); }
    .kv-item dd { margin: 0.2rem 0 0; font-size: 0.8125rem; word-break: break-all; }

    .log-viewer {
      color-scheme: dark;
      background: oklch(0.16 0.005 260);
      color: oklch(0.93 0 0);
      border-radius: var(--radius);
      border: 1px solid oklch(1 0 0 / 0.1);
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
      border: 1px solid var(--hairline);
      border-radius: var(--radius);
      padding: 0.75rem;
      font-family: var(--font-mono);
      font-size: 0.75rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* Login screen (aligned 1:1 with Luma Control AuthLayout.tsx) */
    .auth-screen {
      position: relative;
      isolation: isolate;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 2rem;
      overflow: hidden;
      background: var(--canvas);
      padding: 3rem 1rem;
    }
    .auth-screen::before {
      content: "";
      position: absolute;
      inset: 0;
      z-index: -1;
      pointer-events: none;
      background-image: radial-gradient(var(--border) 1px, transparent 1px);
      background-size: 20px 20px;
      mask-image: radial-gradient(ellipse at center, black 20%, transparent 70%);
    }
    .auth-hero {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.75rem;
    }
    .auth-logo-box {
      width: 3rem;
      height: 3rem;
      border-radius: 0.75rem;
      background: var(--background);
      box-shadow: 0 0 0 1px var(--border), var(--shadow-sm);
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .auth-logo {
      width: 1.75rem;
      height: 1.75rem;
      display: block;
    }
    .auth-title-row {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
    }
    .auth-title-row strong {
      font-size: 1.25rem;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
    .auth-title-row span {
      font-size: 0.875rem;
      color: var(--muted-foreground);
    }
    .auth-card {
      width: min(24rem, 100%);
      background: var(--card);
      border-radius: var(--radius);
      padding: 1.5rem;
      box-shadow: 0 0 0 1px var(--border), var(--shadow-xs);
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    .auth-card-title {
      margin: 0;
      font-size: 0.9375rem;
      font-weight: 600;
      letter-spacing: -0.005em;
    }
    .auth-desc { margin: 0; font-size: 0.8125rem; color: var(--muted-foreground); line-height: 1.5; }
    .auth-footer {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.75rem;
      color: var(--muted-foreground);
    }
    .banner-error {
      padding: 0.625rem 0.75rem;
      border-radius: 0.375rem;
      background: var(--danger-soft);
      color: var(--danger-foreground);
      font-size: 0.8125rem;
      border: 1px solid color-mix(in oklch, var(--danger) 25%, transparent);
    }
    .auth-checking-box {
      display: inline-flex;
      align-items: center;
      gap: 0.625rem;
      font-size: 0.8125rem;
      color: var(--muted-foreground);
    }
    .spinner {
      width: 1rem;
      height: 1rem;
      border-radius: 999px;
      border: 2px solid var(--border);
      border-block-start-color: var(--primary);
      animation: luma-spin 0.75s linear infinite;
    }
    @keyframes luma-spin { to { transform: rotate(360deg); } }

    .table-footer {
      border-block-start: 1px solid var(--hairline);
      padding: 0.75rem var(--card-spacing);
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      background: color-mix(in oklch, var(--muted) 40%, var(--card));
    }
    .pagination-info {
      font-size: 0.75rem;
      color: var(--muted-foreground);
      font-variant-numeric: tabular-nums;
    }
    .pagination-controls {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.375rem;
    }
    .select-sm {
      height: 1.75rem;
      padding-inline: 0.5rem;
      border-radius: 0.375rem;
      font-size: 0.75rem;
    }
    .pagination-page {
      font-size: 0.75rem;
      color: var(--muted-foreground);
      font-variant-numeric: tabular-nums;
      min-width: 5.25rem;
      text-align: center;
    }
    .w-full { width: 100%; }
    .stat-host { font-size: 0.9375rem; line-height: 1.25rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .row-between { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
    .min-w-0 { min-width: 0; }
    .drawer-actions { display: flex; align-items: center; gap: 0.375rem; flex-shrink: 0; }
    .m-0 { margin: 0; }
    .log-heading { font-size: 0.8125rem; font-weight: 600; }
    .stream-hint { font-size: 0.75rem; color: var(--muted-foreground); }
    .stack-sm { display: flex; flex-direction: column; gap: 0.75rem; }
    .ok-note { font-size: 0.8125rem; color: var(--success-foreground); }
    .cell-meta { font-size: 0.75rem; color: var(--muted-foreground); }
    .cell-time { font-size: 0.75rem; color: var(--muted-foreground); white-space: nowrap; }
    .cell-muted { color: var(--muted-foreground); font-size: 0.8125rem; }
    .fw-semi { font-weight: 500; }
    [hidden] { display: none !important; }

    @media (max-width: 820px) {
      .sidebar { display: none; }
      .console-content { padding: 1.25rem 1rem 2.5rem; }
      .field-row { grid-template-columns: 1fr; }
      .sync-status { display: none; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }
  </style>
</head>
<body>
  <div id="auth-checking" class="auth-screen" role="status" aria-live="polite"__AUTH_CHECKING_HIDDEN__>
    <div class="auth-checking-box">
      <span class="spinner" aria-hidden="true"></span>
      <span>正在检查登录状态…</span>
    </div>
  </div>

  <!-- Browser SSO, with explicit Token sign-in for standalone/recovery use. -->
  <main id="auth-view" class="auth-screen"__AUTH_VIEW_HIDDEN__>
    <div class="auth-hero">
      <span class="auth-logo-box">
        <img src="/favicon.png" alt="" class="auth-logo" width="28" height="28">
      </span>
      <div class="auth-title-row">
        <strong>Luma</strong>
        <span>构建控制台</span>
      </div>
    </div>
    <form id="login-form" class="auth-card">
      <div>
        <h1 class="auth-card-title">登录 Luma Build</h1>
        <p id="login-description" class="auth-desc">
          使用 Luma 管理 Token 登录构建控制台。
        </p>
      </div>
      <div id="login-error" class="banner-error" role="alert" hidden></div>
      <a id="control-login" class="btn btn-primary w-full" href="/auth/login" hidden>使用 Luma 控制台登录</a>
      <button id="manual-login" class="btn btn-ghost w-full" type="button" hidden>使用管理 Token</button>
      <div id="token-login-fields" class="stack-sm">
        <div class="field">
          <label for="login-token">访问令牌 (Token)</label>
          <input id="login-token" class="input" type="password" autocomplete="current-password" placeholder="粘贴 LUMA_DEPLOY_TOKEN…" required autofocus>
        </div>
        <label class="check-row">
          <input id="login-remember" type="checkbox" checked>
          <span>在此浏览器记住登录状态</span>
        </label>
        <button type="submit" class="btn btn-primary w-full">进入控制台</button>
      </div>
    </form>
    <footer class="auth-footer">
      <span id="auth-host" translate="no">luma-build</span>
      <span aria-hidden="true">·</span>
      <span class="mono">v__LUMA_BUILD_VERSION__</span>
    </footer>
  </main>

  <!-- Authenticated Console Shell -->
  <div id="app-shell" class="shell"__APP_SHELL_HIDDEN__>
    <aside class="sidebar" aria-label="主导航">
      <div class="sidebar-header">
        <a href="/" class="brand-link" aria-label="Luma Build 控制台首页">
          <span class="brand-mark">
            <img src="/favicon.png" alt="" class="brand-logo" width="20" height="20">
          </span>
          <div class="brand-meta hide-collapsed">
            <strong>Luma Build</strong>
            <span id="sidebar-subtitle">v__LUMA_BUILD_VERSION__</span>
          </div>
        </a>
      </div>

      <div class="sidebar-body">
        <div class="nav-group">
          <div class="nav-group-label hide-collapsed">工作空间</div>
          <ul class="nav-list">
            <li>
              <button type="button" class="nav-btn" data-page="builds" aria-current="page" title="构建记录">
                <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M136,80v43.47l36.12,21.67a8,8,0,0,1-8.24,13.72l-40-24A8,8,0,0,1,120,128V80a8,8,0,0,1,16,0Zm-8-48A95.44,95.44,0,0,0,60.08,60.15C52.81,67.51,46.35,74.59,40,82V64a8,8,0,0,0-16,0v40a8,8,0,0,0,8,8H72a8,8,0,0,0,0-16H49c7.15-8.42,14.27-16.35,22.39-24.57a80,80,0,1,1,1.66,114.75,8,8,0,1,0-11,11.64A96,96,0,1,0,128,32Z"/></svg>
                <span class="hide-collapsed">构建记录</span>
                <span id="nav-active-count" class="nav-badge hide-collapsed">0</span>
              </button>
            </li>
            <li>
              <button type="button" class="nav-btn" data-page="runners" title="Runner 算力池">
                <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M152,96H104a8,8,0,0,0-8,8v48a8,8,0,0,0,8,8h48a8,8,0,0,0,8-8V104A8,8,0,0,0,152,96Zm-8,48H112V112h32Zm88,0H216V112h16a8,8,0,0,0,0-16H216V56a16,16,0,0,0-16-16H160V24a8,8,0,0,0-16,0V40H112V24a8,8,0,0,0-16,0V40H56A16,16,0,0,0,40,56V96H24a8,8,0,0,0,0,16H40v32H24a8,8,0,0,0,0,16H40v40a16,16,0,0,0,16,16H96v16a8,8,0,0,0,16,0V216h32v16a8,8,0,0,0,16,0V216h40a16,16,0,0,0,16-16V160h16a8,8,0,0,0,0-16Zm-32,56H56V56H200v95.87s0,.09,0,.13,0,.09,0,.13V200Z"/></svg>
                <span class="hide-collapsed">Runner 算力池</span>
                <span id="nav-runner-count" class="nav-badge hide-collapsed">0/0</span>
              </button>
            </li>
          </ul>
        </div>

        <div class="nav-group">
          <div class="nav-group-label hide-collapsed">平台管理</div>
          <ul class="nav-list">
            <li>
              <button type="button" class="nav-btn" data-page="settings" title="凭据与配置">
                <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M128,80a48,48,0,1,0,48,48A48.05,48.05,0,0,0,128,80Zm0,80a32,32,0,1,1,32-32A32,32,0,0,1,128,160Zm109.94-52.79a8,8,0,0,0-3.89-5.4l-29.83-17-.12-33.62a8,8,0,0,0-2.83-6.08,111.91,111.91,0,0,0-36.72-20.67,8,8,0,0,0-6.46.59L128,41.85,97.88,25a8,8,0,0,0-6.47-.6A112.1,112.1,0,0,0,54.73,45.15a8,8,0,0,0-2.83,6.07l-.15,33.65-29.83,17a8,8,0,0,0-3.89,5.4,106.47,106.47,0,0,0,0,41.56,8,8,0,0,0,3.89,5.4l29.83,17,.12,33.62a8,8,0,0,0,2.83,6.08,111.91,111.91,0,0,0,36.72,20.67,8,8,0,0,0,6.46-.59L128,214.15,158.12,231a7.91,7.91,0,0,0,3.9,1,8.09,8.09,0,0,0,2.57-.42,112.1,112.1,0,0,0,36.68-20.73,8,8,0,0,0,2.83-6.07l.15-33.65,29.83-17a8,8,0,0,0,3.89-5.4A106.47,106.47,0,0,0,237.94,107.21Zm-15,34.91-28.57,16.25a8,8,0,0,0-3,3c-.58,1-1.19,2.06-1.81,3.06a7.94,7.94,0,0,0-1.22,4.21l-.15,32.25a95.89,95.89,0,0,1-25.37,14.3L134,199.13a8,8,0,0,0-3.91-1h-.19c-1.21,0-2.43,0-3.64,0a8.08,8.08,0,0,0-4.1,1l-28.84,16.1A96,96,0,0,1,67.88,201l-.11-32.2a8,8,0,0,0-1.22-4.22c-.62-1-1.23-2-1.8-3.06a8.09,8.09,0,0,0-3-3.06l-28.6-16.29a90.49,90.49,0,0,1,0-28.26L61.67,97.63a8,8,0,0,0,3-3c.58-1,1.19-2.06,1.81-3.06a7.94,7.94,0,0,0,1.22-4.21l.15-32.25a95.89,95.89,0,0,1,25.37-14.3L122,56.87a8,8,0,0,0,4.1,1c1.21,0,2.43,0,3.64,0a8.08,8.08,0,0,0,4.1-1l28.84-16.1A96,96,0,0,1,188.12,55l.11,32.2a8,8,0,0,0,1.22,4.22c.62,1,1.23,2,1.8,3.06a8.09,8.09,0,0,0,3,3.06l28.6,16.29A90.49,90.49,0,0,1,222.9,142.12Z"/></svg>
                <span class="hide-collapsed">凭据与配置</span>
              </button>
            </li>
          </ul>
        </div>
      </div>

      <div class="sidebar-footer">
        <button type="button" id="sidebar-fleet-btn" class="fleet-summary hide-collapsed" title="查看 Runner 算力池">
          <span id="sidebar-fleet-dot" class="fleet-dot" aria-hidden="true"></span>
          <span id="sidebar-fleet-text" class="fleet-text">等待 Runner 状态…</span>
        </button>
        <button type="button" id="collapse-btn" class="nav-btn" aria-label="收起或展开侧边栏" title="收起侧边栏 (⌘B)">
          <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40ZM40,56H80V200H40ZM216,200H96V56H216V200Z"/></svg>
          <span class="hide-collapsed">收起侧边栏</span>
        </button>
      </div>
    </aside>

    <div class="main-area">
      <header class="topbar">
        <nav class="breadcrumb" aria-label="当前位置">
          <span class="breadcrumb-root">Luma Build</span>
          <span class="breadcrumb-sep" aria-hidden="true">/</span>
          <span id="topbar-page-name" class="breadcrumb-current">构建记录</span>
        </nav>
        <div class="topbar-actions">
          <span id="sync-status" class="sync-status" role="status" hidden>
            <span id="sync-dot" class="sync-dot" aria-hidden="true"></span>
            <span id="sync-text">已同步</span>
          </span>
          <a id="control-link" class="btn btn-sm btn-ghost" href="#" target="_blank" rel="noopener" hidden>
            <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,104a8,8,0,0,1-16,0V59.32l-66.33,66.34a8,8,0,0,1-11.32-11.32L196.68,48H152a8,8,0,0,1,0-16h64a8,8,0,0,1,8,8Zm-40,24a8,8,0,0,0-8,8v72H48V80h72a8,8,0,0,0,0-16H48A16,16,0,0,0,32,80V208a16,16,0,0,0,16,16H176a16,16,0,0,0,16-16V136A8,8,0,0,0,184,128Z"/></svg>
            <span>Luma 控制台</span>
          </a>
          <button type="button" id="theme-toggle" class="btn btn-icon btn-ghost" title="切换浅色 / 深色主题">
            <span class="theme-opt-light">
              <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M120,40V16a8,8,0,0,1,16,0V40a8,8,0,0,1-16,0Zm72,88a64,64,0,1,1-64-64A64.07,64.07,0,0,1,192,128Zm-16,0a48,48,0,1,0-48,48A48.05,48.05,0,0,0,176,128ZM58.34,69.66A8,8,0,0,0,69.66,58.34l-16-16A8,8,0,0,0,42.34,53.66Zm0,116.68-16,16a8,8,0,0,0,11.32,11.32l16-16a8,8,0,0,0-11.32-11.32ZM192,72a8,8,0,0,0,5.66-2.34l16-16a8,8,0,0,0-11.32-11.32l-16,16A8,8,0,0,0,192,72Zm5.66,114.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32-11.32ZM48,128a8,8,0,0,0-8-8H16a8,8,0,0,0,0,16H40A8,8,0,0,0,48,128Zm80,80a8,8,0,0,0-8,8v24a8,8,0,0,0,16,0V216A8,8,0,0,0,128,208Zm112-88H216a8,8,0,0,0,0,16h24a8,8,0,0,0,0-16Z"/></svg>
              <span class="sr-only">切换到浅色主题</span>
            </span>
            <span class="theme-opt-dark">
              <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M233.54,142.23a8,8,0,0,0-8-2,88.08,88.08,0,0,1-109.8-109.8,8,8,0,0,0-10-10,104.84,104.84,0,0,0-52.91,37A104,104,0,0,0,136,224a103.09,103.09,0,0,0,62.52-20.88,104.84,104.84,0,0,0,37-52.91A8,8,0,0,0,233.54,142.23ZM188.9,190.34A88,88,0,0,1,65.66,67.11a89,89,0,0,1,31.4-26A106,106,0,0,0,96,56,104.11,104.11,0,0,0,200,160a106,106,0,0,0,14.92-1.06A89,89,0,0,1,188.9,190.34Z"/></svg>
              <span class="sr-only">切换到深色主题</span>
            </span>
          </button>
          <button type="button" id="logout-btn" class="btn btn-sm btn-ghost">
            <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M120,216a8,8,0,0,1-8,8H48a8,8,0,0,1-8-8V40a8,8,0,0,1,8-8h64a8,8,0,0,1,0,16H56V208h56A8,8,0,0,1,120,216Zm109.66-93.66-40-40a8,8,0,0,0-11.32,11.32L204.69,120H112a8,8,0,0,0,0,16h92.69l-26.35,26.34a8,8,0,0,0,11.32,11.32l40-40A8,8,0,0,0,229.66,122.34Z"/></svg>
            <span>退出</span>
          </button>
        </div>
      </header>

      <div class="console-content">
        <!-- PAGE 1: Build Timeline -->
        <section id="page-builds" class="console-page">
          <div class="page-header">
            <div class="page-header-copy">
              <h1>构建与推送时间线</h1>
              <p>按仓库、分支和状态查询；点击任意记录打开抽屉实时查看 BuildKit 日志与部署进度。</p>
            </div>
            <div class="header-actions">
              <button type="button" id="refresh-builds-btn" class="btn">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,48V96a8,8,0,0,1-8,8H168a8,8,0,0,1,0-16h28.69L182.06,73.37a79.56,79.56,0,0,0-56.13-23.43h-.45A79.52,79.52,0,0,0,69.59,72.71,8,8,0,0,1,58.41,61.27a96,96,0,0,1,135,.79L208,76.69V48a8,8,0,0,1,16,0ZM186.41,183.29a80,80,0,0,1-112.47-.66L59.31,168H88a8,8,0,0,0,0-16H40a8,8,0,0,0-8,8v48a8,8,0,0,0,16,0V179.31l14.63,14.63A95.43,95.43,0,0,0,130,222.06h.53a95.36,95.36,0,0,0,67.07-27.33,8,8,0,0,0-11.18-11.44Z"/></svg>
                <span>刷新</span>
              </button>
              <button type="button" id="open-submit-btn" class="btn btn-primary">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"/></svg>
                <span>从 Git 构建</span>
              </button>
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
              <span class="stat-sub">按页加载历史构建记录</span>
            </div>
            <div class="stat-tile">
              <span class="stat-label">目标镜像仓库 (Registry)</span>
              <strong id="stat-registry" class="stat-value stat-host">—</strong>
              <span id="stat-namespace" class="stat-sub">—</span>
            </div>
          </div>

          <div class="filter-bar" role="search" aria-label="筛选构建记录">
            <div class="search-group">
              <svg class="icon icon-sm search-icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M229.66,218.34l-50.07-50.06a88.11,88.11,0,1,0-11.31,11.31l50.06,50.07a8,8,0,0,0,11.32-11.32ZM40,112a72,72,0,1,1,72,72A72.08,72.08,0,0,1,40,112Z"/></svg>
              <input id="filter-repo" class="input" type="search" aria-label="搜索仓库或分支名称" placeholder="搜索仓库或分支名称…">
            </div>
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
                <tbody id="builds-tbody">
                  <tr class="static-row">
                    <td colspan="4" class="empty-state">正在加载构建记录…</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <nav id="builds-pagination" class="table-footer" aria-label="构建记录分页">
              <span id="builds-range" class="pagination-info">正在加载构建记录…</span>
              <div class="pagination-controls">
                <select id="builds-page-size" class="select select-sm" aria-label="每页显示条数">
                  <option value="20" selected>20 条/页</option>
                  <option value="50">50 条/页</option>
                  <option value="100">100 条/页</option>
                </select>
                <button type="button" id="builds-prev-btn" class="btn btn-sm" disabled>
                  <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M165.66,202.34a8,8,0,0,1-11.32,11.32l-80-80a8,8,0,0,1,0-11.32l80-80a8,8,0,0,1,11.32,11.32L91.31,128Z"/></svg>
                  <span>上一页</span>
                </button>
                <span id="builds-page-info" class="pagination-page">第 1 / 1 页</span>
                <button type="button" id="builds-next-btn" class="btn btn-sm" disabled>
                  <span>下一页</span>
                  <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M181.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L164.69,128,90.34,53.66a8,8,0,0,1,11.32-11.32l80,80A8,8,0,0,1,181.66,133.66Z"/></svg>
                </button>
              </div>
            </nav>
          </div>
        </section>

        <!-- PAGE 2: Runners Pool -->
        <section id="page-runners" class="console-page" hidden>
          <div class="page-header">
            <div class="page-header-copy">
              <h1>Runner 算力池</h1>
              <p>所有算力机通过反向长轮询连接本服务，无需公网入站端口、无需安装 Nomad，与 Luma 控制面完全解耦。</p>
            </div>
            <div class="header-actions">
              <button type="button" id="refresh-runners-btn" class="btn">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,48V96a8,8,0,0,1-8,8H168a8,8,0,0,1,0-16h28.69L182.06,73.37a79.56,79.56,0,0,0-56.13-23.43h-.45A79.52,79.52,0,0,0,69.59,72.71,8,8,0,0,1,58.41,61.27a96,96,0,0,1,135,.79L208,76.69V48a8,8,0,0,1,16,0ZM186.41,183.29a80,80,0,0,1-112.47-.66L59.31,168H88a8,8,0,0,0,0-16H40a8,8,0,0,0-8,8v48a8,8,0,0,0,16,0V179.31l14.63,14.63A95.43,95.43,0,0,0,130,222.06h.53a95.36,95.36,0,0,0,67.07-27.33,8,8,0,0,0-11.18-11.44Z"/></svg>
                <span>刷新</span>
              </button>
              <button type="button" id="open-token-btn" class="btn btn-primary">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"/></svg>
                <span>新建 Runner Token</span>
              </button>
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
            <div class="page-header-copy">
              <h1>凭据与服务配置</h1>
              <p>推荐通过 <code>luma secret set KEY VALUE --scope luma-build</code> 注入环境密钥；也可在此管理动态 Git 与 Registry 凭据。</p>
            </div>
          </div>

          <dl id="settings-summary" class="kv-grid m-0"></dl>

          <div class="card">
            <div class="card-header">
              <strong class="card-title">动态 Git 拉取凭据</strong>
              <button type="button" id="open-git-cred-btn" class="btn btn-sm">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"/></svg>
                <span>添加 Git 凭据</span>
              </button>
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

          <div class="card">
            <div class="card-header">
              <strong class="card-title">动态 OCI Registry Push 凭据</strong>
              <button type="button" id="open-reg-cred-btn" class="btn btn-sm">
                <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"/></svg>
                <span>添加 Registry 凭据</span>
              </button>
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
        <h2 id="drawer-title">—</h2>
        <p id="drawer-subtitle" class="mono">—</p>
      </div>
      <div class="drawer-actions">
        <button type="button" id="drawer-retry-btn" class="btn btn-sm">
          <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M224,48V96a8,8,0,0,1-8,8H168a8,8,0,0,1,0-16h28.69L182.06,73.37a79.56,79.56,0,0,0-56.13-23.43h-.45A79.52,79.52,0,0,0,69.59,72.71,8,8,0,0,1,58.41,61.27a96,96,0,0,1,135,.79L208,76.69V48a8,8,0,0,1,16,0ZM186.41,183.29a80,80,0,0,1-112.47-.66L59.31,168H88a8,8,0,0,0,0-16H40a8,8,0,0,0-8,8v48a8,8,0,0,0,16,0V179.31l14.63,14.63A95.43,95.43,0,0,0,130,222.06h.53a95.36,95.36,0,0,0,67.07-27.33,8,8,0,0,0-11.18-11.44Z"/></svg>
          <span>重试</span>
        </button>
        <button type="button" id="drawer-cancel-btn" class="btn btn-sm btn-danger" hidden>
          <svg class="icon icon-sm" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
          <span>取消构建</span>
        </button>
        <form method="dialog" class="m-0">
          <button type="submit" class="btn btn-icon btn-ghost" aria-label="关闭详情抽屉">
            <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
          </button>
        </form>
      </div>
    </div>
    <div class="drawer-body">
      <dl id="drawer-kv" class="kv-grid kv-grid-muted m-0"></dl>
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
        <button type="button" data-close-dialog="submit-dialog" class="btn btn-icon btn-ghost" aria-label="关闭">
          <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
        </button>
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
        <button type="button" data-close-dialog="token-dialog" class="btn btn-icon btn-ghost" aria-label="关闭">
          <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
        </button>
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
          <strong class="ok-note">已生成令牌（仅显示一次，请妥善复制）：</strong>
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
        <button type="button" data-close-dialog="git-cred-dialog" class="btn btn-icon btn-ghost" aria-label="关闭">
          <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
        </button>
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
        <button type="button" data-close-dialog="reg-cred-dialog" class="btn btn-icon btn-ghost" aria-label="关闭">
          <svg class="icon" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/></svg>
        </button>
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
    const CONTROL_SSO = __LUMA_CONTROL_SSO__;
    const INITIAL_SESSION_STATE = __LUMA_SESSION_STATE__;
    const SCHEME_KEY = "luma-build-color-scheme";
    const state = {
      token: "",
      session: false,
      page: "builds",
      statusFilter: "",
      repoFilter: "",
      pageSize: 20,
      pageIndex: 1,
      buildsTotal: 0,
      buildsLoaded: false,
      buildsReqSeq: 0,
      repoDebounce: null,
      statusData: null,
      builds: [],
      selectedBuildId: "",
      streamAbort: null,
      pollTimer: null,
    };

    const authHostEl = document.getElementById("auth-host");
    if (authHostEl && window.location.host) authHostEl.textContent = window.location.host;

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

    // Two-state color-scheme toggle synced with <meta name="color-scheme"> and theme-color
    const schemeMeta = document.querySelector('meta[name="color-scheme"]');
    const themeMeta = document.querySelector('meta[name="theme-color"]');
    const darkMedia = window.matchMedia("(prefers-color-scheme: dark)");
    function currentRenderedScheme() {
      const content = schemeMeta.content;
      if (content === "dark" || content === "light") return content;
      return darkMedia.matches ? "dark" : "light";
    }
    function loginHref() {
      return `/auth/login?theme=${encodeURIComponent(currentRenderedScheme())}`;
    }
    function syncThemeChrome() {
      const rendered = currentRenderedScheme();
      if (themeMeta) themeMeta.content = rendered === "dark" ? "#0a0a0a" : "#f5f3f3";
      const link = document.getElementById("control-login");
      if (link) link.href = loginHref();
    }
    document.getElementById("theme-toggle").addEventListener("click", () => {
      const target = currentRenderedScheme() === "dark" ? "light" : "dark";
      const sysScheme = darkMedia.matches ? "dark" : "light";
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
      syncThemeChrome();
    });
    window.addEventListener("storage", (ev) => {
      if (ev.key === SCHEME_KEY) {
        schemeMeta.content = ev.newValue === "light" || ev.newValue === "dark" ? ev.newValue : "light dark";
        syncThemeChrome();
      }
    });
    darkMedia.addEventListener("change", syncThemeChrome);
    syncThemeChrome();

    function fmtTime(epoch) {
      if (!epoch) return "—";
      const d = new Date(Number(epoch) * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }

    function fmtClock(date) {
      const pad = (n) => String(n).padStart(2, "0");
      return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function updateSyncIndicator(ok) {
      const wrap = document.getElementById("sync-status");
      const dot = document.getElementById("sync-dot");
      const text = document.getElementById("sync-text");
      if (!wrap || !dot || !text) return;
      wrap.hidden = false;
      dot.classList.toggle("is-err", !ok);
      text.textContent = ok ? `已更新 ${fmtClock(new Date())}` : "同步失败";
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
      const [label, cls] = map[status] || [status || "—", "pill pill-dot pill-neutral"];
      return el("span", { className: cls }, label);
    }

    async function api(path, opts = {}) {
      const res = await fetch(path, {
        method: opts.method || "GET",
        headers: {
          ...(state.token ? { "Authorization": `Bearer ${state.token}` } : {}),
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

    function showAuthView(errorMsg = "") {
      document.getElementById("auth-checking").hidden = true;
      document.getElementById("app-shell").hidden = true;
      document.getElementById("auth-view").hidden = false;
      syncThemeChrome();
      const errEl = document.getElementById("login-error");
      if (errorMsg) {
        errEl.textContent = errorMsg;
        errEl.hidden = false;
      } else {
        errEl.hidden = true;
      }
    }

    function signOut(errorMsg = "") {
      state.token = "";
      state.session = false;
      if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
      if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
      try { sessionStorage.removeItem(TOKEN_KEY); localStorage.removeItem(TOKEN_KEY); } catch {}
      showAuthView(errorMsg);
    }

    document.getElementById("logout-btn").addEventListener("click", async () => {
      if (state.session) {
        try {
          const response = await fetch("/v1/auth/logout", { method: "POST" });
          if (!response.ok && response.status !== 401) throw new Error("退出失败，请稍后重试");
        } catch (err) {
          alert(err.message || "退出失败，请稍后重试");
          return;
        }
      }
      history.replaceState(null, "", "?signedOut=1");
      signOut();
    });
    document.getElementById("manual-login").addEventListener("click", () => {
      history.replaceState(null, "", "?login=manual");
      document.getElementById("token-login-fields").hidden = false;
      document.getElementById("manual-login").hidden = true;
      document.getElementById("login-token").focus();
    });
    document.getElementById("collapse-btn").addEventListener("click", () => {
      document.getElementById("app-shell").classList.toggle("collapsed");
    });
    window.addEventListener("keydown", (ev) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "b" && !document.getElementById("app-shell").hidden) {
        ev.preventDefault();
        document.getElementById("app-shell").classList.toggle("collapsed");
      }
    });

    // Navigation
    const pageNames = { builds: "构建记录", runners: "Runner 算力池", settings: "凭据与配置" };
    function navigateTo(targetPage) {
      state.page = targetPage;
      for (const b of document.querySelectorAll(".nav-btn[data-page]")) {
        if (b.getAttribute("data-page") === targetPage) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
      }
      document.getElementById("topbar-page-name").textContent = pageNames[state.page] || "";
      document.getElementById("page-builds").hidden = state.page !== "builds";
      document.getElementById("page-runners").hidden = state.page !== "runners";
      document.getElementById("page-settings").hidden = state.page !== "settings";
      refreshAll();
    }
    for (const btn of document.querySelectorAll(".nav-btn[data-page]")) {
      btn.addEventListener("click", () => navigateTo(btn.getAttribute("data-page")));
    }
    document.getElementById("sidebar-fleet-btn").addEventListener("click", () => navigateTo("runners"));

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
        state.session = false;
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
      document.getElementById("auth-checking").hidden = true;
      document.getElementById("auth-view").hidden = true;
      document.getElementById("app-shell").hidden = false;
      refreshAll();
      if (!state.pollTimer) {
        state.pollTimer = setInterval(() => {
          if (document.visibilityState === "visible" && (state.token || state.session)) refreshAll();
        }, 4000);
      }
    }

    async function refreshBuilds() {
      const reqSeq = ++state.buildsReqSeq;
      try {
        const bl = await api(buildListUrl());
        if (reqSeq !== state.buildsReqSeq) return;
        const total = typeof bl.total === "number" ? bl.total : (bl.builds || []).length;
        const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
        if (state.pageIndex > totalPages) {
          state.pageIndex = totalPages;
          await refreshBuilds();
          return;
        }
        state.builds = bl.builds || [];
        state.buildsTotal = total;
        state.buildsLoaded = true;
        renderBuildsTable(state.builds);
        renderBuildsPagination();
      } catch {}
    }

    async function refreshAll() {
      try {
        if (state.page === "builds") {
          const [st] = await Promise.all([
            api("/v1/status"),
            refreshBuilds(),
          ]);
          state.statusData = st;
          renderSummary(st);
          renderRunnersTable(st.runners || []);
        } else if (state.page === "settings") {
          const [st, gc, rc] = await Promise.all([
            api("/v1/status"),
            api("/v1/credentials/git"),
            api("/v1/credentials/registry"),
          ]);
          state.statusData = st;
          renderSummary(st);
          renderRunnersTable(st.runners || []);
          renderSettings(st, gc.credentials || [], rc.credentials || []);
        } else {
          const st = await api("/v1/status");
          state.statusData = st;
          renderSummary(st);
          renderRunnersTable(st.runners || []);
        }
        updateSyncIndicator(true);
      } catch {
        updateSyncIndicator(false);
      }
    }

    function buildListUrl() {
      const offset = Math.max(0, (state.pageIndex - 1) * state.pageSize);
      const p = new URLSearchParams({
        limit: String(state.pageSize),
        offset: String(offset),
      });
      if (state.statusFilter) p.set("status", state.statusFilter);
      if (state.repoFilter) p.set("repo", state.repoFilter);
      return `/v1/builds?${p}`;
    }

    function renderBuildsPagination() {
      const total = state.buildsTotal;
      const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
      const rangeEl = document.getElementById("builds-range");
      const pageInfoEl = document.getElementById("builds-page-info");
      const prevBtn = document.getElementById("builds-prev-btn");
      const nextBtn = document.getElementById("builds-next-btn");
      if (!state.buildsLoaded) {
        rangeEl.textContent = "正在加载构建记录…";
      } else if (!total) {
        rangeEl.textContent = "共 0 条记录";
      } else {
        const start = (state.pageIndex - 1) * state.pageSize + 1;
        const end = Math.min(start + state.builds.length - 1, total);
        rangeEl.textContent = `显示第 ${start}–${end} 条，共 ${total} 条`;
      }
      pageInfoEl.textContent = `第 ${state.pageIndex} / ${totalPages} 页`;
      prevBtn.disabled = state.pageIndex <= 1;
      nextBtn.disabled = state.pageIndex >= totalPages;
    }

    function renderSummary(st) {
      const c = st.buildCounts || {};
      const active = (c.queued || 0) + (c.building || 0) + (c.queued_deploy || 0) + (c.deploying || 0);
      const succ = c.succeeded || 0;
      const fail = c.failed || 0;
      const runners = (st.runners || []).filter((r) => !r.revoked);
      const online = runners.filter((r) => r.online);
      const activeBadge = document.getElementById("nav-active-count");
      activeBadge.textContent = String(active);
      activeBadge.classList.toggle("is-active", active > 0);
      document.getElementById("nav-runner-count").textContent = `${online.length}/${runners.length}`;

      const fleetDot = document.getElementById("sidebar-fleet-dot");
      const fleetText = document.getElementById("sidebar-fleet-text");
      fleetDot.classList.remove("tone-ok", "tone-warn");
      if (!runners.length) {
        fleetText.textContent = "暂无已注册 Runner";
      } else {
        fleetDot.classList.add(online.length === runners.length ? "tone-ok" : "tone-warn");
        fleetText.textContent = `${online.length}/${runners.length} Runner 就绪`;
      }

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
                "点击右上角「从 Git 构建」或在终端执行 luma builder submit 发起首次构建。"
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
              el("span", { className: "record-title", title: b.repoSlug }, b.repoSlug),
              el("span", { className: "record-sub mono", title: subtitle }, subtitle)
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
                "点击右上角「新建 Runner Token」即可把任意 Linux 主机或家里 Mac 接入编译池。"
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
            el("td", { className: "mono" }, r.nativePlatform || "—"),
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
        ["认证模式", st.authMode || "—"],
        ["Luma Control 部署目标", (st.controlTarget && st.controlTarget.endpoint) || "未启用（仅构建模式）"],
        ["默认 Pull Registry", (st.registry && st.registry.registryHost) || "—"],
        ["默认 Push Registry", (st.registry && st.registry.pushHost) || "—"],
        ["Registry 命名空间", (st.registry && st.registry.namespace) || "—"],
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
      document.getElementById("drawer-stream-state").textContent = "实时日志流连接中…";
      const ctrl = new AbortController();
      state.streamAbort = ctrl;
      let seenSeq = (b.events || []).reduce((m, e) => Math.max(m, e.seq || 0), 0);
      try {
        const res = await fetch(`/v1/builds/${encodeURIComponent(buildId)}/stream`, {
          headers: state.token ? { "Authorization": `Bearer ${state.token}` } : {},
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
        ["目标平台", (b.platforms || []).join(", ") || "—"],
        ["源码提交", (b.result && b.result.commit) ? b.result.commit.slice(0, 12) : b.ref],
        ["产物镜像", (b.result && b.result.image) || "—"],
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
      state.pageIndex = 1;
      if (state.repoDebounce) clearTimeout(state.repoDebounce);
      state.repoDebounce = setTimeout(() => {
        state.repoDebounce = null;
        refreshBuilds();
      }, 250);
    });
    for (const seg of document.querySelectorAll("#status-segments .seg-btn")) {
      seg.addEventListener("click", () => {
        state.statusFilter = seg.getAttribute("data-status") || "";
        state.pageIndex = 1;
        for (const s of document.querySelectorAll("#status-segments .seg-btn")) {
          s.setAttribute("aria-pressed", s === seg ? "true" : "false");
        }
        refreshBuilds();
      });
    }
    document.getElementById("builds-prev-btn").addEventListener("click", () => {
      if (state.pageIndex > 1) {
        state.pageIndex -= 1;
        refreshBuilds();
      }
    });
    document.getElementById("builds-next-btn").addEventListener("click", () => {
      const totalPages = Math.max(1, Math.ceil(state.buildsTotal / state.pageSize));
      if (state.pageIndex < totalPages) {
        state.pageIndex += 1;
        refreshBuilds();
      }
    });
    document.getElementById("builds-page-size").addEventListener("change", (ev) => {
      const next = Number(ev.target.value) || 20;
      state.pageSize = Math.max(1, Math.min(next, 200));
      state.pageIndex = 1;
      refreshBuilds();
    });

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
        state.pageIndex = 1;
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

    // Prefer the HttpOnly session. A saved management token must not silently
    // bypass Control sign-out or its passkey policy when browser SSO is enabled.
    async function boot() {
      const query = new URLSearchParams(location.search);
      const manual = query.get("login") === "manual";
      if (CONTROL_SSO) {
        document.getElementById("control-login").hidden = false;
        document.getElementById("manual-login").hidden = manual;
        document.getElementById("token-login-fields").hidden = !manual;
        document.getElementById("login-description").textContent = "共用 Luma 控制台登录状态，支持通行密钥登录。";
        syncThemeChrome();
        if (INITIAL_SESSION_STATE === "authenticated") {
          state.session = true;
          enterShell();
          return;
        }
        if (!manual) {
          if (query.get("sso") === "failed") {
            signOut("Luma 登录未完成或已过期，请重新登录。");
            return;
          }
          if (query.has("signedOut")) {
            signOut();
            return;
          }
          if (INITIAL_SESSION_STATE === "expired") {
            location.replace(loginHref());
            return;
          }
          if (INITIAL_SESSION_STATE === "unavailable") {
            showAuthView("Luma 登录服务暂不可用，请稍后重试。");
            return;
          }
          try {
            const response = await fetch("/v1/auth/session", { credentials: "same-origin" });
            if (response.ok && (await response.json()).authenticated) {
              state.session = true;
              enterShell();
              return;
            }
            if (response.status !== 401) throw new Error("Luma 登录服务暂不可用，请稍后重试。");
          } catch (err) {
            showAuthView(err.message || "Luma 登录服务暂不可用，请稍后重试。");
            return;
          }
          location.replace(loginHref());
          return;
        }
      }
      const existing = loadStoredToken();
      if (existing) {
        state.token = existing;
        try { await api("/v1/status"); enterShell(); return; } catch { signOut(); return; }
      }
      showAuthView();
    }
    boot();
  })();
  </script>
</body>
</html>
"""
