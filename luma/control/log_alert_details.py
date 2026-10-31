"""Small incident evidence snapshots; no queries, files or growing sample history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from ..log_alert_rules import bounded_samples, clean_sample_text


def snapshot(rule, sample):
    query = rule['logQuery']
    at = sample['sampledAt']
    return dict(app=rule['target'], task=query.get('task', ''),
                windowSeconds=query['windowSeconds'], windowStart=at-query['windowSeconds'],
                windowEnd=at, sampledAt=at, matchCount=sample['value'], total=sample.get('total'),
                query={k: clean_sample_text(v, 256) for k, v in query.items() if k in ('contains', 'exclude', 'field', 'equals') and v},
                samples=bounded_samples(sample.get('samples')))


def links(details, incident_id, domain):
    if not domain or urlsplit('https://'+domain).netloc != domain or any(c in domain for c in '/?#@ \n\r'):
        return {}
    origin = 'https://' + domain + '/dashboard'
    return dict(incidentUrl=f"{origin}/apps/{quote(details['app'], safe='')}/alerts/{incident_id}",
                logsUrl=f"{origin}/apps/{quote(details['app'], safe='')}/logs")


def detail_rows(details):
    tz = timezone(timedelta(hours=8))
    stamp = lambda n: datetime.fromtimestamp(n, tz).strftime('%Y-%m-%d %H:%M:%S')
    rows = [('应用 / 服务', details['app'] + ' / ' + (details.get('task') or '全部服务')),
            ('异常窗口 UTC+08:00', f"{stamp(details['windowStart'])} — {stamp(details['windowEnd'])}"),
            ('窗口内匹配', f"{details['matchCount']:g} 条；展示 {len(details.get('samples', []))} 条样例（最多 3 条）")]
    for index, sample in enumerate(details.get('samples', [])):
        source = ' · '.join(filter(None, [sample.get('_time'), sample.get('task'), sample.get('stream')]))
        rows.append((f'异常样例 {index+1}', source + '\n' + sample.get('_msg', '')))
    return rows
