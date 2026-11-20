#!/usr/bin/env python3
"""Build the static Pages artifact, including readable Markdown documentation.

Requires Markdown==3.8.2. Run from any directory; output defaults to _site.
"""
from __future__ import annotations

import argparse
import datetime
import functools
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import markdown
from markdown.extensions.toc import slugify

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_URL = 'https://liutianjie.github.io/luma/'
DEFAULT_REPO_URL = 'https://github.com/LiuTianjie/luma'


def normalize_site_url(url: str) -> str:
    return url.rstrip('/') + '/'


def normalize_repo_url(url: str) -> str:
    return url.rstrip('/')


# Project Pages base URL; canonical, hreflang, Open Graph and sitemap URLs must be absolute.
SITE_URL = normalize_site_url(os.environ.get('LUMA_SITE_URL', DEFAULT_SITE_URL))
REPO_URL = normalize_repo_url(os.environ.get('LUMA_REPO_URL', DEFAULT_REPO_URL))

MENU_TOGGLE_SVG = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M4 6h16M4 12h16M4 18h16"/></svg>'
)

GUIDES = [
    ('README', 'Overview'),
    ('getting-started', 'Getting started'),
    ('concepts', 'Core concepts'),
    ('deploying', 'Deploying applications'),
    ('operations', 'Operations'),
    ('addons', 'Addons & SSO'),
    ('runners', 'Build runners'),
    ('deployment-yaml', 'Deployment YAML'),
    ('compose-storage', 'Compose & storage'),
    ('control-storage', 'Control storage'),
    ('secrets', 'Secrets & credentials'),
    ('nomad-security', 'Nomad security'),
    ('exposure-model', 'Exposure model'),
    ('egress-gateway', 'Egress gateway'),
    ('dns-zones', 'DNS zones'),
    ('cdn', 'CDN'),
    ('observability', 'Observability'),
    ('dashboard-guide', 'Console guide'),
    ('troubleshooting', 'Troubleshooting'),
    ('luma-cli-reference', 'CLI reference'),
    ('ai-agent-skills', 'AI Agent Skills'),
]

ZH_LABELS = [
    '概览',
    '快速上手',
    '核心概念',
    '部署应用',
    '日常运维',
    '插件与统一登录',
    '构建 Runner',
    '部署 YAML',
    'Compose 与存储',
    '控制面存储',
    '密钥与凭据',
    'Nomad 安全',
    '暴露模型',
    '出网网关',
    'DNS 域名',
    'CDN',
    '可观测',
    '控制台指南',
    '故障排查',
    'CLI 命令参考',
    'AI Agent Skills',
]

assert len(GUIDES) == len(ZH_LABELS), 'GUIDES and ZH_LABELS must have identical lengths'


def chinese_source(source: Path) -> bool:
    # Explicit translations or original Chinese prose; ignore command/help blocks.
    prose = re.sub(r'^```.*?^```[^\n]*', '', source.read_text(encoding='utf-8'), flags=re.M | re.S)
    return (
        '.zh-CN.' in source.name
        or len(re.findall(r'[\u4e00-\u9fff]', prose)) >= 100
        and not source.with_name(source.stem + '.zh-CN.md').exists()
    )


def localized_source(source: Path) -> Path:
    translated = source.with_name(source.stem + '.zh-CN.md')
    return translated if translated.is_file() else source


@functools.lru_cache(maxsize=None)
def target_heading_ids(target: Path) -> frozenset[str]:
    rendered = markdown.markdown(target.read_text(encoding='utf-8'), extensions=['fenced_code', 'toc', 'attr_list'])
    return frozenset(re.findall(r'<h[1-6] id="([^"]+)"', rendered))


def rewrite_link(match: re.Match[str], source: Path, chinese: bool = False, repo_url: str = REPO_URL) -> str:
    attribute, value = match.group(1), html.unescape(match.group(2))
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith('/'):
        return match.group(0)
    target = (source.parent / parts.path).resolve()
    if target.suffix == '.md' and target.is_relative_to(ROOT / 'docs') and target.is_file():
        target = localized_source(target) if chinese else target
        relative = Path(os.path.relpath(target, source.parent)).with_suffix('.html').as_posix()
        fragment = parts.fragment
        if fragment:
            fragment = unquote(fragment)
            ids = target_heading_ids(target)
            if fragment not in ids:
                fallback = re.sub(r'-+', '-', slugify(fragment, '-')).strip('-')
                if fallback in ids:
                    fragment = fallback
        value = urlunsplit(('', '', relative, parts.query, fragment))
    elif target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'docs'):
        # Repository source and folders live on GitHub, never at missing Pages paths.
        rel = target.relative_to(ROOT).as_posix()
        kind = 'tree' if target.is_dir() else 'blob'
        value = f'{repo_url}/{kind}/main/{rel}'
        if parts.fragment:
            value += '#' + parts.fragment
    return f'{attribute}="{html.escape(value, quote=True)}"'


def last_modified(*paths: Path) -> str:
    # Pages checks out full history (fetch-depth: 0) so this is the file's own last commit.
    try:
        dates = subprocess.run(
            ['git', '-C', str(ROOT), 'log', '-1', '--format=%cs', '--', *map(str, paths)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        dates = ''
    return dates or datetime.date.today().isoformat()


def summary(content: str, chinese: bool, fallback: str) -> str:
    # The first real paragraph makes a better search snippet than the bare title.
    for paragraph in re.findall(r'<p>(.*?)</p>', content, re.S):
        # Language-switch links read as noise in a search snippet.
        paragraph = re.sub(r'<a [^>]*>\s*(?:简体中文|中文|English)\s*</a>', '', paragraph)
        text = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', paragraph))).strip()
        if len(text) >= (30 if chinese else 60):
            break
    else:
        return fallback
    limit = 90 if chinese else 155
    if len(text) > limit:
        cut = text[: limit - 1]
        text = (cut if chinese or ' ' not in cut else cut.rsplit(' ', 1)[0]).rstrip(' ,.;:，。；：') + '…'
    return text


def json_ld(data: dict) -> str:
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/') + '</script>'


def alternate_links(urls: dict[str, str]) -> str:
    links = ''.join(f'<link rel="alternate" hreflang="{lang}" href="{url}">' for lang, url in urls.items())
    return links + (f'<link rel="alternate" hreflang="x-default" href="{urls["en"]}">' if 'en' in urls else '')


def write_sitemap(output: Path, entries: list[tuple[str, str, dict[str, str]]], site_url: str = SITE_URL) -> None:
    rows = []
    for url, modified, alternates in entries:
        links = ''.join(f'<xhtml:link rel="alternate" hreflang="{lang}" href="{href}"/>' for lang, href in alternates.items())
        if 'en' in alternates:
            links += f'<xhtml:link rel="alternate" hreflang="x-default" href="{alternates["en"]}"/>'
        rows.append(f'  <url><loc>{url}</loc><lastmod>{modified}</lastmod>{links}</url>')
    (output / 'sitemap.xml').write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        + '\n'.join(rows)
        + '\n</urlset>\n',
        encoding='utf-8',
    )
    # Crawlers only read robots.txt at the host root; this copy documents the sitemap location for project Pages.
    (output / 'robots.txt').write_text(f'User-agent: *\nAllow: /\n\nSitemap: {site_url}sitemap.xml\n', encoding='utf-8')


def write_not_found(output: Path, asset_version: str, site_url: str = SITE_URL) -> None:
    # GitHub Pages serves this at any missing depth, so relative URLs resolve against <base>.
    (output / '404.html').write_text(
        f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><meta name="theme-color" content="#fafafa"><base href="{site_url}"><title>Page not found · Luma</title><link rel="icon" type="image/png" sizes="32x32" href="./favicon-32.png"><link rel="stylesheet" href="./styles.css?v={asset_version}"></head><body><header class="site-header"><div class="nav-shell"><a class="brand" href="./"><img src="./assets/luma-logo-mark.png" width="32" height="32" alt=""><span>Luma</span></a></div></header><main id="main" class="hero wrap"><div class="hero-copy"><h1>Page not found</h1><p class="hero-lede">This page does not exist or has moved. <span lang="zh-CN">页面不存在或已移动。</span></p><div class="hero-actions"><a class="button primary" href="./">Home</a><a class="text-link" href="./zh.html" lang="zh-CN">中文首页</a><a class="text-link" href="./docs/README.html">Documentation</a><a class="text-link" href="./docs/README.zh-CN.html" lang="zh-CN">文档</a></div></div></main></body></html>''',
        encoding='utf-8',
    )


def build(output: Path, site_url: str = SITE_URL, repo_url: str = REPO_URL) -> None:
    site_url = normalize_site_url(site_url)
    repo_url = normalize_repo_url(repo_url)
    og_image = {'en': site_url + 'assets/og-image.png', 'zh-CN': site_url + 'assets/og-image-zh.png'}
    target_heading_ids.cache_clear()

    output = output.resolve()
    if output == ROOT or ROOT.is_relative_to(output) or output.is_relative_to(ROOT / 'site') or output.is_relative_to(ROOT / 'docs'):
        raise ValueError('Output must not overwrite source directories')
    marker = output / '.luma-site-output'
    if output.exists() and any(output.iterdir()):
        if not marker.is_file():
            raise ValueError('Refusing to replace an unrecognized nonempty output directory')
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    marker.touch()
    shutil.copytree(ROOT / 'site', output, dirs_exist_ok=True)
    shutil.copytree(ROOT / 'docs', output / 'docs', dirs_exist_ok=True, ignore=shutil.ignore_patterns('research'))
    asset_version = hashlib.sha256((ROOT / 'site/styles.css').read_bytes() + (ROOT / 'site/app.js').read_bytes()).hexdigest()[:10]
    for landing in ('index.html', 'zh.html'):
        page = output / landing
        rendered = page.read_text(encoding='utf-8')
        if site_url != DEFAULT_SITE_URL:
            rendered = rendered.replace(DEFAULT_SITE_URL, site_url)
        if repo_url != DEFAULT_REPO_URL:
            rendered = rendered.replace(DEFAULT_REPO_URL, repo_url)
        rendered = re.sub(r'((?:href|src)="(?:\./)?(?:styles\.css|app\.js))"', rf'\1?v={asset_version}"', rendered)
        page.write_text(rendered, encoding='utf-8')
    landing_alternates = {'en': site_url, 'zh-CN': site_url + 'zh.html'}
    sitemap = [(url, last_modified(ROOT / 'site'), landing_alternates) for url in landing_alternates.values()]
    count = 0
    for source in sorted((ROOT / 'docs').rglob('*.md')):
        rel = source.relative_to(ROOT / 'docs')
        if 'research' in rel.parts:
            continue
        dest = output / 'docs' / rel.with_suffix('.html')
        prefix = '../' * (len(rel.parts))
        doc_prefix = '../' * (len(rel.parts) - 1)
        raw = source.read_text(encoding='utf-8')
        chinese = chinese_source(source)
        renderer = markdown.Markdown(
            extensions=['fenced_code', 'tables', 'toc', 'sane_lists', 'attr_list'],
            extension_configs={'toc': {'permalink': '#', 'toc_depth': '2-3'}},
        )
        content = renderer.convert(raw)
        content = re.sub(r'(href|src)="([^"]*)"', lambda match: rewrite_link(match, source, chinese, repo_url), content)
        if chinese:
            content = content.replace('title="Permanent link"', 'title="章节链接"')
        content = content.replace('<table>', '<div class="doc-table"><table>').replace('</table>', '</table></div>')
        heading = re.search(r'^#\s+(.+)$', raw, re.MULTILINE)
        title = html.escape(re.sub(r'\s*\{#[^}]*\}$', '', heading.group(1)) if heading else source.stem)
        sidebar = ''
        for index, (slug, label) in enumerate(GUIDES):
            target = localized_source(ROOT / 'docs' / (slug + '.md')) if chinese else ROOT / 'docs' / (slug + '.md')
            active = source == target
            attrs = ' class="active" aria-current="page"' if active else ''
            sidebar += f'<a{attrs} href="{doc_prefix}{target.stem}.html">{ZH_LABELS[index] if chinese else label}</a>'
        language = 'zh-CN' if chinese else 'en'
        home = prefix + ('zh.html' if chinese else 'index.html')
        original = source.with_name(source.name.replace('.zh-CN.md', '.md'))
        counterpart = original if '.zh-CN.' in source.name else localized_source(source)
        switch = (
            f'<a class="language" href="{counterpart.with_suffix(".html").name}" data-doc-language lang="{"en" if chinese else "zh-CN"}">{"English" if chinese else "中文"}</a>'
            if counterpart != source
            else ''
        )
        labels = (
            ['跳到正文', '文档', '展开导航', '主导航', '控制台', '插件', '架构', '文档', '指南', '在 GitHub 查看源码', '原始 Markdown', '本页目录']
            if chinese
            else ['Skip to content', 'Documentation', 'Toggle navigation', 'Main navigation', 'Console', 'Addons', 'Architecture', 'Documentation', 'Guides', 'View source on GitHub', 'Raw Markdown', 'On this page']
        )
        skip, documentation, toggle, navigation, console, addons_label, arch_label, guides, guide_label, view_source, raw_label, toc_label = labels
        page_title = f'{title} · Luma {documentation}'
        if len(html.unescape(page_title)) > 60:
            page_title = f'{title} · Luma'
        plain_title = html.unescape(title)
        description = html.escape(summary(content, chinese, f'{plain_title} — Luma {documentation}'), quote=True)
        url = site_url + 'docs/' + rel.with_suffix('.html').as_posix()
        alternates = {language: url}
        if counterpart != source:
            alternates['en' if chinese else 'zh-CN'] = site_url + 'docs/' + counterpart.relative_to(ROOT / 'docs').with_suffix('.html').as_posix()
        alternates = dict(sorted(alternates.items(), key=lambda item: item[0] != 'en'))
        modified = last_modified(source)
        sitemap.append((url, modified, alternates if len(alternates) > 1 else {}))
        home_url = site_url + ('zh.html' if chinese else '')
        docs_url = site_url + 'docs/' + ('README.zh-CN.html' if chinese else 'README.html')
        crumbs = [('Luma', home_url), (documentation, docs_url)] + ([] if url == docs_url else [(plain_title, url)])
        structured = json_ld({
            '@context': 'https://schema.org',
            '@graph': [
                {
                    '@type': 'TechArticle',
                    'headline': plain_title,
                    'description': html.unescape(description),
                    'inLanguage': language,
                    'url': url,
                    'dateModified': modified,
                    'isPartOf': {'@type': 'WebSite', 'name': 'Luma', 'url': site_url},
                    'publisher': {'@type': 'Organization', 'name': 'Luma', 'url': site_url, 'logo': site_url + 'assets/luma-logo.png'},
                },
                {
                    '@type': 'BreadcrumbList',
                    'itemListElement': [{'@type': 'ListItem', 'position': index + 1, 'name': name, 'item': item} for index, (name, item) in enumerate(crumbs)],
                },
            ],
        })
        locale = 'zh_CN' if chinese else 'en_US'
        seo_head = (
            f'<meta name="description" content="{description}"><meta name="theme-color" content="#fafafa"><link rel="canonical" href="{url}">'
            + (alternate_links(alternates) if len(alternates) > 1 else '')
            + f'<meta property="og:type" content="article"><meta property="og:site_name" content="Luma"><meta property="og:title" content="{page_title}"><meta property="og:description" content="{description}"><meta property="og:url" content="{url}"><meta property="og:locale" content="{locale}"><meta property="og:image" content="{og_image[language]}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta property="og:image:alt" content="Luma"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{page_title}"><meta name="twitter:description" content="{description}"><meta name="twitter:image" content="{og_image[language]}">'
            + f'<link rel="icon" type="image/png" sizes="32x32" href="{prefix}favicon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="{prefix}apple-touch-icon.png">{structured}'
        )
        dest.write_text(
            f'''<!doctype html><html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{page_title}</title>{seo_head}<link rel="stylesheet" href="{prefix}styles.css?v={asset_version}"><script src="{prefix}app.js?v={asset_version}" defer></script></head><body><a class="skip-link" href="#doc">{skip}</a><header class="site-header"><div class="nav-shell"><a class="brand" href="{home}"><img src="{prefix}assets/luma-logo-mark.png" width="30" height="30" alt=""><span>Luma</span></a><span class="brand-caption">{documentation}</span><button type="button" class="menu-toggle" aria-label="{toggle}" aria-expanded="false" aria-controls="navigation">{MENU_TOGGLE_SVG}</button><nav id="navigation" aria-label="{navigation}"><a href="{home}#product">{console}</a><a href="{home}#addons">{addons_label}</a><a href="{home}#architecture">{arch_label}</a><a href="{home}#agent-skills">Skills</a><a href="{home}#docs">{guides}</a>{switch}<a class="button small" href="{repo_url}">GitHub ↗</a></nav></div></header><main class="docs-layout"><nav class="docs-sidebar" aria-label="{documentation}"><strong>{guide_label}</strong>{sidebar}</nav><article class="doc-article" id="doc"><div class="doc-breadcrumb"><a href="{home}#docs">{documentation}</a><span>/</span><span>{title}</span></div>{content}<div class="doc-source"><a href="{repo_url}/blob/main/docs/{rel.as_posix()}">{view_source} ↗</a> · <a href="./{source.name}">{raw_label}</a></div></article><aside class="docs-toc" aria-label="{toc_label}"><strong>{toc_label}</strong>{renderer.toc}</aside></main></body></html>''',
            encoding='utf-8',
        )
        count += 1
    write_sitemap(output, sitemap, site_url)
    write_not_found(output, asset_version, site_url)
    print(f'Built site and {count} documentation pages in {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '_site')
    parser.add_argument('--site-url', default=SITE_URL, help='Base site URL (or set LUMA_SITE_URL)')
    parser.add_argument('--repo-url', default=REPO_URL, help='Repository URL (or set LUMA_REPO_URL)')
    args = parser.parse_args()
    build(args.output, site_url=args.site_url, repo_url=args.repo_url)
