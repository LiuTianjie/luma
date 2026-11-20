#!/usr/bin/env python3
"""Check the built Pages artifact for missing local assets/links, SEO metadata, and landing-page parity."""
from __future__ import annotations

import argparse
import importlib.util
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Page(HTMLParser):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.links: list[str] = []
        self.ids: set[str] = set()
        self.sections: set[str] = set()
        self.seo_urls: list[tuple[str, str]] = []
        self.feed(path.read_text(encoding='utf-8'))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: v for k, v in attrs if v is not None}
        if 'id' in attributes:
            self.ids.add(attributes['id'])
            if tag == 'section':
                self.sections.add(attributes['id'])
        for key in ('href', 'src'):
            if key in attributes:
                self.links.append(attributes[key])
        if tag == 'link':
            rel = attributes.get('rel', '')
            href = attributes.get('href', '')
            if href and rel == 'canonical':
                self.seo_urls.append(('canonical', href))
            elif href and rel == 'alternate' and 'hreflang' in attributes:
                self.seo_urls.append((f'hreflang={attributes["hreflang"]}', href))
        elif tag == 'meta':
            prop = attributes.get('property', '')
            content = attributes.get('content', '')
            if prop == 'og:image' and content:
                self.seo_urls.append(('og:image', content))


def extract_fences(text: str) -> list[str]:
    return re.findall(r'^```[^\n]*\n.*?^```[^\n]*', text, re.M | re.S)


def site_url_to_path(root: Path, site_url: str, url: str) -> Path | None:
    if not url.startswith(site_url):
        return None
    rel = urlsplit(url[len(site_url):]).path
    rel = unquote(rel).lstrip('/')
    target = (root / rel).resolve() if rel else root / 'index.html'
    if target.is_dir():
        target = target / 'index.html'
    return target


def main() -> None:
    source_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('site_builder', source_root / 'scripts/build-site.py')
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=source_root / '_site')
    parser.add_argument('--site-url', default=builder.SITE_URL, help='Expected base site URL (or set LUMA_SITE_URL)')
    args = parser.parse_args()

    root = args.output.resolve()
    site_url = builder.normalize_site_url(args.site_url)
    pages = {path: Page(path) for path in root.rglob('*.html')}
    errors: list[str] = []

    for path, page in pages.items():
        for link in page.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            if parsed.path.startswith('/'):
                errors.append(f'{path.relative_to(root)}: absolute local URL breaks project Pages: {link}')
                continue
            target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if target.is_dir():
                target = target / 'index.html'
            if not target.exists():
                errors.append(f'{path.relative_to(root)}: missing {link}')
            if parsed.fragment and target in pages and unquote(parsed.fragment) not in pages[target].ids:
                errors.append(f'{path.name}: missing anchor {link}')
        for kind, seo_url in page.seo_urls:
            target = site_url_to_path(root, site_url, seo_url)
            if target is not None and (not target.is_relative_to(root) or not target.is_file()):
                errors.append(f'{path.relative_to(root)}: {kind} URL does not map to built file: {seo_url}')

    for name in ('index.html', 'zh.html'):
        if root / name not in pages:
            errors.append(f'Missing {name}')
    if not errors and pages[root / 'index.html'].sections != pages[root / 'zh.html'].sections:
        errors.append('Landing page sections differ between languages')

    # Validate sitemap.xml
    sitemap_path = root / 'sitemap.xml'
    if not sitemap_path.is_file():
        errors.append('Missing sitemap.xml')
    else:
        try:
            tree = ET.fromstring(sitemap_path.read_text(encoding='utf-8'))
        except ET.ParseError as exc:
            errors.append(f'sitemap.xml is not valid XML: {exc}')
        else:
            ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9', 'xhtml': 'http://www.w3.org/1999/xhtml'}
            locs = [el.text.strip() for el in tree.findall('sm:url/sm:loc', ns) if el.text]
            if not locs:
                errors.append('sitemap.xml contains no <loc> entries')
            for expected in (site_url, site_url + 'zh.html'):
                if expected not in locs:
                    errors.append(f'sitemap.xml missing expected landing URL: {expected}')
            for loc in locs:
                target = site_url_to_path(root, site_url, loc)
                if target is None or not target.is_relative_to(root) or not target.is_file():
                    errors.append(f'sitemap.xml <loc> does not map to built file: {loc}')
            for alt in tree.findall('sm:url/xhtml:link', ns):
                href = alt.attrib.get('href', '')
                target = site_url_to_path(root, site_url, href)
                if target is None or not target.is_relative_to(root) or not target.is_file():
                    errors.append(f'sitemap.xml alternate href does not map to built file: {href}')

    # Validate robots.txt
    robots_path = root / 'robots.txt'
    if not robots_path.is_file():
        errors.append('Missing robots.txt')
    else:
        robots_text = robots_path.read_text(encoding='utf-8')
        if 'User-agent:' not in robots_text or 'Allow:' not in robots_text:
            errors.append('robots.txt missing User-agent or Allow directive')
        expected_sitemap_line = f'Sitemap: {site_url}sitemap.xml'
        if expected_sitemap_line not in robots_text:
            errors.append(f'robots.txt missing expected line: {expected_sitemap_line}')

    # Chinese is required for all public documentation, including original Chinese files.
    for source in sorted((source_root / 'docs').rglob('*.md')):
        relative = source.relative_to(source_root / 'docs')
        if 'research' in relative.parts or '.zh-CN.' in source.name:
            continue
        translated = builder.localized_source(source)
        if not builder.chinese_source(translated):
            errors.append(f'{relative}: missing Chinese translation')
            continue
        output_path = root / 'docs' / translated.relative_to(source_root / 'docs').with_suffix('.html')
        if output_path not in pages:
            errors.append(f'{relative}: missing Chinese HTML')
            continue
        if '<html lang="zh-CN">' not in output_path.read_text(encoding='utf-8'):
            errors.append(f'{relative}: Chinese document has an English shell')
        if translated != source:
            if extract_fences(source.read_text(encoding='utf-8')) != extract_fences(translated.read_text(encoding='utf-8')):
                errors.append(f'{relative}: translated command/code examples differ')
            original_page = root / 'docs' / relative.with_suffix('.html')
            original_ids = pages[original_page].ids
            if not original_ids <= pages[output_path].ids:
                errors.append(f'{relative}: translation loses heading anchors {original_ids - pages[output_path].ids}')
        for link in pages[output_path].links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path.endswith('.html'):
                continue
            target = (output_path.parent / parsed.path).resolve()
            cn_target = target.with_name(target.stem + '.zh-CN.html')
            if cn_target.exists():
                # The explicit English language switch is the sole allowed English link.
                original_page = root / 'docs' / relative.with_suffix('.html')
                if target != original_page or translated == source:
                    errors.append(f'{relative}: Chinese page links to English document {link}')

    for path in pages:
        match = re.search(r'<title>(.*?)</title>', path.read_text(encoding='utf-8'), re.S)
        if match and '{#' in match.group(1):
            errors.append(f'{path.name}: heading attribute leaked into page title')

    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'Validated local links, assets, sitemap, robots.txt and SEO URLs across {len(pages)} HTML pages; bilingual sections match.')


if __name__ == '__main__':
    main()
