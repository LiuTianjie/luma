"""Expiring, rule-scoped notification silences for exact observable sources."""
from __future__ import annotations

import json

from ..alert_sources import SILENCE_SECONDS, bounded_sources, sample_source, source
from ..errors import LumaError


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS alert_source_silences(
        rule_id TEXT NOT NULL,
        target TEXT NOT NULL, source_key TEXT NOT NULL, source_kind TEXT NOT NULL,
        source_name TEXT NOT NULL, until_at REAL NOT NULL, created_at REAL NOT NULL,
        actor TEXT NOT NULL, PRIMARY KEY(rule_id,target,source_key))''')
    conn.execute('CREATE INDEX IF NOT EXISTS alert_source_silences_expiry ON alert_source_silences(until_at)')


def incident_sources(rule, incident):
    """Use aggregate identities for logs and the configured target for metrics."""
    if rule['metric'] == 'app.log_matches':
        details = json.loads(incident['log_details'])
        return bounded_sources(details.get('sources'), details.get('matchCount'))
    query = json.loads(rule['task_query'])
    identity = (source('task', query['taskName']) if query.get('taskName') else
                source('service', query['service']) if query.get('service') else
                source('target', incident['target']))
    return [{**identity, 'count': 1}] if identity else []


def notification_view(conn, rule, incident, now):
    """Filter only notifications; raw measurements still drive incident recovery."""
    if incident['no_data']:
        return {'sources': [], 'keys': ['no_data'], 'details': json.loads(incident['log_details'])}
    sources = incident_sources(rule, incident)
    silences = {r['source_key']: r['until_at'] for r in conn.execute(
        'SELECT source_key,until_at FROM alert_source_silences WHERE rule_id=? AND target=? AND until_at>?',
        (rule['id'], incident['target'], now))}
    visible = [item for item in sources if item['key'] not in silences]
    details = json.loads(incident['log_details'])
    keys = [item['key'] for item in visible]
    if rule['metric'] == 'app.log_matches':
        total = details.get('matchCount', incident['value'])
        if not isinstance(total, (int, float)):
            return {'sources': [], 'keys': ['unknown'], 'details': details}
        muted = sum(item['count'] for item in sources if item['key'] in silences)
        remaining = total - muted
        if remaining <= rule['threshold']:
            return None
        unknown = total - sum(item['count'] for item in sources)
        if unknown > 0:
            # Unknown/overflow sources stay visible and cannot inherit another mute.
            keys.append('unknown')
            if sources:
                keys.append('overflow:' + str(details.get('sampledAt', '')))
        details = {**details, 'matchCount': remaining, 'sources': visible,
                   'silencedMatchCount': muted,
                   'samples': [item for item in details.get('samples', [])
                               if not (sample_source(item) or {}).get('key') in silences]}
    elif sources and not visible:
        return None
    return {'sources': visible, 'keys': keys or ['unknown'], 'details': details}


def notification_kind(conn, rule, incident, now):
    """A different unmuted source bypasses another source's repeat interval."""
    view = notification_view(conn, rule, incident, now)
    if view is None:
        return None
    previous = json.loads(incident['notification_sources'])
    if incident['last_notified_at'] is None or any(item['key'] not in previous for item in view['sources']):
        return 'firing'
    if (now - incident['last_notified_at'] >= rule['repeat_seconds']
            or any(key not in previous for key in view['keys'])):
        return 'reminder'
    return None


def delivery_is_silenced(conn, delivery, now):
    """Recheck persisted matchers when recovering an interrupted delivery lease."""
    try:
        payload = json.loads(delivery['text'])
        keys = {item['key'] for item in payload.get('sources', [])}
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return any(row['source_key'] in keys for row in conn.execute('''
        SELECT s.source_key FROM alert_source_silences s JOIN alert_incidents i
        ON i.rule_id=s.rule_id AND i.target=s.target WHERE i.id=? AND s.until_at>?''',
        (delivery['incident_id'], now)))


def silence(conn, rule, incident, source_key, seconds, now, *, actor='dashboard'):
    """Persist one source matcher and invalidate only deliveries containing it."""
    if isinstance(seconds, bool) or seconds not in SILENCE_SECONDS:
        raise LumaError('Choose a silence of 1, 6 or 24 hours')
    if incident['status'] not in ('pending', 'firing'):
        raise LumaError('This incident has ended; refresh before silencing a source')
    selected = next((item for item in incident_sources(rule, incident) if item['key'] == source_key), None)
    if selected is None:
        raise LumaError('Alert source is unavailable; refresh the incident')
    until = now + seconds
    conn.execute('''INSERT INTO alert_source_silences VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(rule_id,target,source_key) DO UPDATE SET until_at=excluded.until_at,
        created_at=excluded.created_at,actor=excluded.actor''',
        (rule['id'], incident['target'], source_key, selected['kind'], selected['name'], until, now, actor))
    previous = json.loads(incident['notification_sources'])
    conn.execute('UPDATE alert_incidents SET notification_sources=? WHERE id=?',
                 (json.dumps([key for key in previous if key != source_key]), incident['id']))
    # Rebuild mixed-source queued cards on the next evaluation instead of sending
    # a stale card or cancelling the other source's notification permanently.
    for delivery in conn.execute("SELECT id,text FROM alert_outbox WHERE incident_id=? AND kind IN ('firing','reminder') AND status IN ('pending','retry')", (incident['id'],)).fetchall():
        payload = json.loads(delivery['text'])
        if any(item.get('key') == source_key for item in payload.get('sources', [])):
            conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Alert source silenced' WHERE id=?", (delivery['id'],))
            conn.execute("UPDATE alert_incidents SET notification_sources='[]' WHERE id=?", (incident['id'],))
    conn.execute('INSERT INTO alert_events(incident_id,kind,at,detail) VALUES(?,?,?,?)',
                 (incident['id'], 'source_silenced', now, json.dumps({**selected, 'until': until, 'actor': actor}, ensure_ascii=False)))
    return {**selected, 'until': until}


def list_silences(conn, now, *, application=''):
    """Return active matchers with enough identity to revoke exactly one scope."""
    query = '''SELECT s.*,r.name AS rule_name FROM alert_source_silences s
        JOIN alert_rules r ON r.id=s.rule_id WHERE s.until_at>?'''
    args = [now]
    if application:
        query += " AND r.target=? AND (r.metric LIKE 'app.%' OR r.metric='build.failed')"
        args.append(application)
    return [dict(ruleId=r['rule_id'], ruleName=r['rule_name'], target=r['target'],
                 sourceKey=r['source_key'], kind=r['source_kind'], name=r['source_name'],
                 until=r['until_at']) for r in conn.execute(query + ' ORDER BY s.until_at LIMIT 200', args)]


def remove(conn, body, now, *, application=''):
    """Revoke the exact saved matcher and let its next fresh observation notify."""
    rule = conn.execute('SELECT * FROM alert_rules WHERE id=?', (body.get('ruleId'),)).fetchone()
    if not rule or (application and rule['target'] not in (application, '*')):
        raise LumaError('alert rule not found')
    target, key = body.get('target'), body.get('sourceKey')
    if application and target != application:
        raise LumaError('alert target does not match application')
    removed = conn.execute('DELETE FROM alert_source_silences WHERE rule_id=? AND target=? AND source_key=?',
                           (rule['id'], target, key)).rowcount
    if removed:
        for incident in conn.execute("SELECT * FROM alert_incidents WHERE rule_id=? AND target=? AND status IN ('pending','firing')", (rule['id'], target)):
            keys = json.loads(incident['notification_sources'])
            conn.execute('UPDATE alert_incidents SET notification_sources=? WHERE id=?',
                         (json.dumps([item for item in keys if item != key]), incident['id']))
            conn.execute('INSERT INTO alert_events(incident_id,kind,at,detail) VALUES(?,?,?,?)',
                         (incident['id'], 'source_unsilenced', now, key))
    return {'removed': bool(removed)}
