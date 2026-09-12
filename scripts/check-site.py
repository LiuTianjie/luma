#!/usr/bin/env python3
"""Check the built Pages artifact for missing local assets/links and landing-page parity."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import re
import importlib.util

class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.path, self.links, self.ids, self.sections = path, [], set(), set()
        self.feed(path.read_text(encoding='utf-8'))
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if 'id' in attributes:
            self.ids.add(attributes['id'])
            if tag == 'section': self.sections.add(attributes['id'])
        for key in ('href', 'src'):
            if key in attributes: self.links.append(attributes[key])

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / '_site')
args = parser.parse_args()
root = args.output.resolve()
pages = {path: Page(path) for path in root.rglob('*.html')}
errors = []
for path, page in pages.items():
    for link in page.links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc: continue
        if parsed.path.startswith('/'):
            errors.append(f'{path.relative_to(root)}: absolute local URL breaks project Pages: {link}')
            continue
        target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        if target.is_dir(): target = target / 'index.html'
        if not target.exists(): errors.append(f'{path.relative_to(root)}: missing {link}')
        if parsed.fragment and target in pages and unquote(parsed.fragment) not in pages[target].ids:
            errors.append(f'{path.name}: missing anchor {link}')
for name in ('index.html', 'zh.html'):
    if root / name not in pages: errors.append(f'Missing {name}')
if not errors and pages[root / 'index.html'].sections != pages[root / 'zh.html'].sections:
    errors.append('Landing page sections differ between languages')
# Chinese is required for all public documentation, including original Chinese files.
source_root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_builder', source_root / 'scripts/build-site.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
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
    if '<html lang="zh-CN">' not in output_path.read_text():
        errors.append(f'{relative}: Chinese document has an English shell')
    if translated != source:
        fences = lambda text: re.findall(r'^```[^\n]*\n.*?^```[^\n]*', text, re.M | re.S)
        if fences(source.read_text()) != fences(translated.read_text()):
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
for path, page in pages.items():
    if '{#' in re.search(r'<title>(.*?)</title>', path.read_text(), re.S)[1]:
        errors.append(f'{path.name}: heading attribute leaked into page title')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'Validated local links and assets across {len(pages)} HTML pages; bilingual sections match.')
