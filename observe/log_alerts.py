#!/usr/bin/env python3
"""Isolated, diskless LogsQL aggregation. No query runs in the Control process."""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from log_alert_rules import INTERVAL, MAX_AGE, MAX_RULES, compile_query, fingerprint, normalize

MAX_BODY = 65536
QUERY_TIMEOUT = 2


def query_logs(rule, now):
    with urllib.request.urlopen('http://127.0.0.1:9108/coverage', timeout=0.5) as response:
        coverage = json.loads(response.read(65536))
    selected = [r for r in coverage if r['app'] == rule['target']
                and (not rule['logQuery']['task'] or r['task'] == rule['logQuery']['task'])]
    if not selected or any(not -5 <= now-r['at'] < 120 or
                           (r.get('gapAt', 0) and now-r['gapAt'] < rule['logQuery']['windowSeconds'])
                           for r in selected):
        raise ValueError('Log collection is not caught up')
    base, condition = compile_query(rule['target'], rule['logQuery'])
    query = base + f' | stats count() as total, count() if ({condition}) as matches, row_any(_msg) if ({condition}) as sample'
    if rule['logQuery']['field']:
        query += f", count(parsed.{rule['logQuery']['field']}) as parsed"
    params = urllib.parse.urlencode({'query': query, 'start': now-rule['logQuery']['windowSeconds'],
                                    'end': now, 'timeout': '1s'})
    req = urllib.request.Request('http://127.0.0.1:9428/select/logsql/query', data=params.encode())
    with urllib.request.urlopen(req, timeout=QUERY_TIMEOUT) as response:
        raw = response.read(49153)
    if len(raw) > 49152:
        raise ValueError('Oversized aggregate')
    rows = [json.loads(line) for line in raw.splitlines() if line]
    if len(rows) != 1:
        raise ValueError('Missing aggregate')
    total, matches = float(rows[0]['total']), float(rows[0]['matches'])
    if not all(math.isfinite(v) and v >= 0 for v in (total, matches)) or matches > total:
        raise ValueError('Invalid aggregate')
    # No source logs is unknown, never automatic recovery. Idle services may stay unknown.
    if rule['logQuery']['field'] and total and not float(rows[0].get('parsed', 0)):
        return {'status': 'invalid_format', 'value': None, 'total': total, 'sampledAt': now}
    sample = json.loads(rows[0].get('sample') or '{}').get('_msg', '')
    return {'value': matches if total else None, 'total': total, 'sampledAt': now,
            'sample': str(sample)[:512],
            'status': 'ok' if total else 'no_source_logs'}


class Engine:
    """One serial query worker, at most 20 rules, one cached preview, no files."""
    def __init__(self, query=query_logs, clock=time.time):
        self.query, self.clock = query, clock
        self.lock = threading.Lock()
        self.rules, self.results = {}, {}
        self.last_sync = 0
        self.preview = None
        self.last_preview = 0

    def exchange(self, items):
        if not isinstance(items, list) or len(items) > MAX_RULES:
            raise ValueError('At most 20 log rules')
        rules = {}
        for item in items:
            target = item['target']
            config = normalize(target, item['logQuery'])
            key = fingerprint(target, config)
            rules[key] = {'target': target, 'logQuery': config}
        with self.lock:
            self.last_sync = self.clock()
            self.rules = rules
            self.results = {k: v for k, v in self.results.items() if k in rules}
            return {'results': {k: v for k, v in self.results.items()
                                if self.clock()-v['sampledAt'] <= MAX_AGE}}

    def request_preview(self, item):
        config = normalize(item['target'], item['logQuery'])
        key = fingerprint(item['target'], config)
        with self.lock:
            now = self.clock()
            if self.preview and self.preview['key'] == key and now-self.preview['at'] < INTERVAL:
                return self.preview.get('result', {'status': 'pending'})
            if now-self.last_preview < 10:
                return {'status': 'busy'}
            self.last_preview = now
            self.preview = {'key': key, 'at': now, 'rule': {'target': item['target'], 'logQuery': config}}
            return {'status': 'pending'}

    def step(self):
        with self.lock:
            now = self.clock()
            if now-self.last_sync > MAX_AGE:
                self.rules, self.results = {}, {}
            work = [(key, rule, False) for key, rule in self.rules.items()
                    if now-self.results.get(key, {}).get('sampledAt', 0) >= INTERVAL]
            if self.preview and 'result' not in self.preview:
                work.append((self.preview['key'], self.preview['rule'], True))
            work.sort(key=lambda item: self.preview['at'] if item[2] else self.results.get(item[0], {}).get('sampledAt', 0))
        # One query per step, at most one per 2 seconds globally. Preview cannot starve rules.
        if not work:
            return
        key, rule, preview = work[0]
        try:
            result = self.query(rule, now)
        except Exception:
            result = {'status': 'unavailable', 'value': None, 'sampledAt': now}
        with self.lock:
            if preview:
                if self.preview and self.preview['key'] == key:
                    self.preview['result'] = result
            elif key in self.rules:
                self.results[key] = result


class Handler(BaseHTTPRequestHandler):
    engine: Engine

    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY:
                raise ValueError('Request too large')
            body = json.loads(self.rfile.read(size))
            if self.path == '/sync':
                result = self.engine.exchange(body['rules'])
            elif self.path == '/preview':
                result = self.engine.request_preview(body)
            else:
                self.send_error(404)
                return
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (ValueError, KeyError, TypeError):
            self.send_error(400, 'Invalid log rule request')

    def setup(self):
        super().setup()
        self.connection.settimeout(2)


def main():
    Handler.engine = Engine()
    def run():
        while True:
            Handler.engine.step()
            time.sleep(2)
    threading.Thread(target=run, daemon=True).start()
    # A single bounded HTTP handler serves only in-memory state; no query or file IO here.
    HTTPServer(('127.0.0.1', 9109), Handler).serve_forever()


if __name__ == '__main__':
    main()
