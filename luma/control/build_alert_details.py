"""Allowlisted metadata and bounded error summaries for build alerts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import yaml


def clean(value, limit=500, *, keep_tail=False):
    text = str(value or '')
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(?i)(\b(?:password|token|secret|authorization|api[_-]?key)\b[\s"\x27]*[:=][\s"\x27]*)([^\s,;"\x27]+)', r'\1[redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
    if len(text) <= limit:
        return text
    if keep_tail:
        return text[:limit//3] + '\n…\n' + text[-(limit*2//3):]
    return text[:limit] + '…'


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _yaml(value):
    if not isinstance(value, str) or len(value) > 262144:
        return {}
    try:
        return _mapping(yaml.safe_load(value))
    except (yaml.YAMLError, ValueError, RecursionError):
        return {}


def result_fields(result):
    """Small scalar progress fields, also retained if subsequent deployment fails."""
    if not isinstance(result, dict):
        return {}
    manifest = _yaml(result.get('manifest'))
    fields = {}
    for key,value in {'service':result.get('service') or result.get('deployment') or manifest.get('name'),
                      'image':result.get('image')}.items():
        if isinstance(value,str) and value:
            fields[key] = clean(value)
    images = {clean(k,100):clean(v) for k,v in list(_mapping(result.get('images')).items())[:20] if isinstance(v,str)}
    if images:
        # Progress fields are capped at 4000 characters by the event store.
        # Keep valid JSON rather than truncating an entire image map away.
        while len(json.dumps(images,ensure_ascii=False)) > 3500:
            images.pop(next(reversed(images)))
        fields['images'] = json.dumps(images,ensure_ascii=False)
    return fields


def event_context(events):
    context = {}
    for event in events:
        for key in ('service','image'):
            if isinstance(event.get(key),str) and event[key]:
                context[key] = clean(event[key])
        if isinstance(event.get('images'),str):
            try:
                context['images'] = {clean(k,100):clean(v) for k,v in list(_mapping(json.loads(event['images'])).items())[:20] if isinstance(v,str)}
            except ValueError:
                pass
        match = re.fullmatch(r'Built and pushed (\S+)', str(event.get('message') or ''))
        if match:
            context['image'] = clean(match[1])
    return context


def build_details(run, events, domain=''):
    request, result = _mapping(run.get('request')), _mapping(run.get('result'))
    manifest = _yaml(request.get('manifest'))
    build = _mapping(manifest.get('build'))
    events = [e for e in events if isinstance(e, dict)]
    failed = next((e for e in events if e.get('status') in ('fail', 'failed', 'error')), {})
    context = {**_mapping(run.get('buildContext')), **event_context(events)}
    service = result.get('service') or result.get('deployment') or context.get('service') or request.get('name') or request.get('application') or manifest.get('name') or run.get('applicationRef') or run.get('slug')
    image = result.get('image') or context.get('image') or request.get('image') or manifest.get('image')
    if isinstance(image, dict):
        image = image.get('selected') or image.get('requested')
    images = _mapping(result.get('images')) or _mapping(context.get('images'))
    identifier = str(run.get('id') or '')
    details = {key: clean(value, 1200 if key == 'reason' else 500, keep_tail=key == 'reason') for key, value in {
        'buildId': identifier, 'service': service, 'image': image,
        'imageRepository': build.get('repo') or request.get('imageRepo'),
        'buildNode': run.get('buildNode') or request.get('buildNode'),
        'repository': request.get('repoUrl') or request.get('repository') or run.get('source'),
        'ref': request.get('ref') or run.get('plannedCommit'),
        'stage': failed.get('name'), 'reason': failed.get('message') or run.get('message'),
    }.items() if isinstance(value, (str, int, float)) and value != ''}
    if images:
        details['images'] = {clean(k, 100): clean(v) for k, v in list(images.items())[:20] if isinstance(v, str)}
    for key in ('createdAt', 'completedAt'):
        value = run.get(key)
        if isinstance(value, (int, float)) and 0 < value < 253402300800:
            details[key] = value
    failed_at = failed.get('ts') or run.get('completedAt') or run.get('updatedAt')
    if isinstance(failed_at, (int, float)) and 0 < failed_at < 253402300800:
        details['failedAt'] = failed_at
    # The origin comes from the configured cluster domain, never a request Host.
    if isinstance(domain, str) and domain and not re.search(r'[\s/?#@]', domain):
        parsed = urlsplit('https://' + domain)
        if parsed.hostname and identifier:
            details['url'] = 'https://' + domain + '/dashboard/deployments/build/' + quote(identifier, safe='')
    return details


def detail_rows(details):
    rows = [('服务 / 应用', details.get('service') or '未记录'),
            ('构建编号', details.get('buildId') or '未记录'),
            ('镜像完整引用', details.get('image') or '未记录（未生成或历史记录未保存）')]
    for key, title in [('imageRepository', '目标镜像仓库'), ('buildNode', '构建节点'),
                       ('repository', '源码仓库'), ('ref', '分支 / 提交'), ('stage', '失败阶段')]:
        if details.get(key):
            rows.append((title, details[key]))
    for service, image in (details.get('images') or {}).items():
        rows.append((f'镜像 · {service}', image))
    if details.get('failedAt'):
        rows.append(('失败时间', datetime.fromtimestamp(details['failedAt'], timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S UTC+08:00')))
    rows.append(('失败原因', details.get('reason') or '未记录，请查看完整构建日志'))
    return rows
