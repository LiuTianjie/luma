#!/usr/bin/env python3
"""Build the static Pages artifact, including readable Markdown documentation.

Requires Markdown==3.8.2. Run from any directory; output defaults to _site.
"""
from pathlib import Path
import argparse
import html
import hashlib
import os
import re
import shutil
from urllib.parse import urlsplit, urlunsplit
import markdown

ROOT = Path(__file__).resolve().parents[1]
GUIDES = [('dashboard-guide', 'Console guide'), ('how-to-use-luma', 'Operating manual'), ('bootstrap', 'Install & bootstrap'), ('architecture', 'Architecture'), ('concepts', 'Core concepts'), ('deployment-yaml', 'Deployment YAML'), ('compose-storage', 'Compose & storage'), ('observability', 'Observability'), ('secrets', 'Secrets & credentials'), ('luma-cli', 'CLI reference'), ('troubleshooting', 'Troubleshooting'), ('ai-agent-skills', 'AI Agent Skills')]

ZH_LABELS = ['控制台指南', '使用手册', '安装与初始化', '系统架构', '核心概念', '部署 YAML', 'Compose 与存储', '可观测', '密钥与凭据', 'CLI 命令参考', '故障排查', 'AI Agent Skills']

def chinese_source(source):
    # Explicit translations or original Chinese prose; ignore command/help blocks.
    prose = re.sub(r'^```.*?^```[^\n]*', '', source.read_text(), flags=re.M | re.S)
    return '.zh-CN.' in source.name or len(re.findall(r'[\u4e00-\u9fff]', prose)) >= 100 and not source.with_name(source.stem + '.zh-CN.md').exists()

def localized_source(source):
    translated = source.with_name(source.stem + '.zh-CN.md')
    return translated if translated.is_file() else source

def rewrite_link(match, source, chinese=False):
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
            from urllib.parse import unquote
            from markdown.extensions.toc import slugify
            fragment = unquote(fragment)
            rendered_target = markdown.markdown(target.read_text(), extensions=['fenced_code', 'toc', 'attr_list'])
            ids = set(re.findall(r'<h[1-6] id="([^"]+)"', rendered_target))
            if fragment not in ids:
                fallback = re.sub(r'-+', '-', slugify(fragment, '-')).strip('-')
                if fallback in ids:
                    fragment = fallback
        value = urlunsplit(('', '', relative, parts.query, fragment))
    elif target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'docs'):
        # Repository source and folders live on GitHub, never at missing Pages paths.
        rel = target.relative_to(ROOT).as_posix()
        kind = 'tree' if target.is_dir() else 'blob'
        value = f'https://github.com/LiuTianjie/luma/{kind}/main/{rel}'
        if parts.fragment:
            value += '#' + parts.fragment
    return f'{attribute}="{html.escape(value, quote=True)}"'

def build(output):
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
    asset_version = hashlib.sha256((ROOT / "site/styles.css").read_bytes() + (ROOT / "site/app.js").read_bytes()).hexdigest()[:10]
    for landing in ('index.html', 'zh.html'):
        page = output / landing
        rendered = re.sub(r'((?:href|src)="(?:\./)?(?:styles\.css|app\.js))"', rf'\1?v={asset_version}"', page.read_text())
        page.write_text(rendered)
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
        renderer = markdown.Markdown(extensions=['fenced_code', 'tables', 'toc', 'sane_lists', 'attr_list'], extension_configs={'toc': {'permalink': '#', 'toc_depth': '2-3'}})
        content = renderer.convert(raw)
        content = re.sub(r'(href|src)="([^"]*)"', lambda match: rewrite_link(match, source, chinese), content)
        if chinese:
            content = content.replace('title="Permanent link"', 'title="章节链接"')
        content = content.replace('<table>', '<div class="doc-table"><table>').replace('</table>', '</table></div>')
        heading = re.search(r'^#\s+(.+)$', raw, re.MULTILINE)
        title = html.escape(re.sub(r'\s*\{#[^}]*\}$', '', heading.group(1)) if heading else source.stem)
        sidebar = ''
        for index, (slug, label) in enumerate(GUIDES):
            target = localized_source(ROOT / 'docs' / (slug + '.md')) if chinese else ROOT / 'docs' / (slug + '.md')
            active = source == target
            sidebar += f'<a class="{"active" if active else ""}" {"aria-current=page" if active else ""} href="{doc_prefix}{target.stem}.html">{ZH_LABELS[index] if chinese else label}</a>'
        language = 'zh-CN' if chinese else 'en'
        home = prefix + ('zh.html' if chinese else 'index.html')
        original = source.with_name(source.name.replace('.zh-CN.md', '.md'))
        counterpart = original if '.zh-CN.' in source.name else localized_source(source)
        switch = f'<a href="{counterpart.with_suffix(".html").name}" data-doc-language lang="{"en" if chinese else "zh-CN"}">{"English" if chinese else "中文"}</a>' if counterpart != source else ''
        labels = ['跳到正文', '文档', '展开导航', '主导航', '控制台', '全部指南', '指南', '在 GitHub 查看源码', '原始 Markdown', '本页目录'] if chinese else ['Skip to content', 'Documentation', 'Toggle navigation', 'Main navigation', 'Console', 'All guides', 'Guides', 'View source on GitHub', 'Raw Markdown', 'On this page']
        skip, documentation, toggle, navigation, console, guides, guide_label, view_source, raw_label, toc_label = labels
        dest.write_text(f'''<!doctype html><html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · Luma {documentation}</title><meta name="description" content="{title} — Luma {documentation}"><link rel="icon" href="{prefix}favicon-32.png"><link rel="stylesheet" href="{prefix}styles.css?v={asset_version}"><script src="{prefix}app.js?v={asset_version}" defer></script></head><body><a class="skip-link" href="#doc">{skip}</a><header class="site-header"><div class="nav-shell"><a class="brand" href="{home}"><img src="{prefix}assets/luma-logo-mark.png" width="30" height="30" alt=""><span>Luma</span></a><span class="brand-caption">{documentation}</span><button class="menu-toggle" aria-label="{toggle}" aria-expanded="false" aria-controls="navigation">☰</button><nav id="navigation" aria-label="{navigation}"><a href="{home}#product">{console}</a><a href="{home}#docs">{guides}</a>{switch}<a class="button small" href="https://github.com/LiuTianjie/luma">GitHub ↗</a></nav></div></header><main class="docs-layout"><nav class="docs-sidebar" aria-label="{documentation}"><strong>{guide_label}</strong>{sidebar}</nav><article class="doc-article" id="doc"><div class="doc-breadcrumb"><a href="{home}#docs">{documentation}</a><span>/</span><span>{title}</span></div>{content}<div class="doc-source"><a href="https://github.com/LiuTianjie/luma/blob/main/docs/{rel.as_posix()}">{view_source} ↗</a> · <a href="./{source.name}">{raw_label}</a></div></article><aside class="docs-toc" aria-label="{toc_label}"><strong>{toc_label}</strong>{renderer.toc}</aside></main></body></html>''', encoding='utf-8')
        count += 1
    print(f'Built site and {count} documentation pages in {output}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '_site')
    args = parser.parse_args()
    build(args.output)
