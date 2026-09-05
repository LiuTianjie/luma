"""Single-Manager persistent alert evaluation and leased notification outbox.

The evaluator consumes persisted samples; it never probes Nomad or sends while
holding a SQLite transaction. Delivery is at-least-once: a crash after the bot
accepts a message but before our commit can duplicate that delivery.
"""
from __future__ import annotations

import json
import hashlib
import math
import secrets
import time
from typing import Any, Callable

from ..errors import LumaError
from . import database
from .alert_notifications import FeishuError, send_feishu, validate_credentials

PRESETS = [
    dict(metric='node.offline', name='节点失联', description='Agent 心跳距今超过阈值；不是 Nomad 调度状态', threshold=120, forSeconds=60, unit='seconds'),
    dict(metric='node.cpu', name='CPU 持续高占用', description='新鲜的 Linux 主机 CPU 样本', threshold=90, forSeconds=300, unit='percent'),
    dict(metric='node.memory', name='内存持续高占用', description='主机内存使用率', threshold=85, forSeconds=300, unit='percent'),
    dict(metric='node.disk', name='磁盘空间紧张', description='Agent 所采样文件系统使用率', threshold=90, forSeconds=300, unit='percent'),
    dict(metric='node.inode', name='inode 空间紧张', description='Agent 所采样文件系统 inode 使用率', threshold=90, forSeconds=300, unit='percent'),
    dict(metric='task.queue_age', name='任务排队过久', description='按 agent/builder/build 分组的最老排队任务等待秒数', threshold=600, forSeconds=60, unit='seconds'),
    dict(metric='build.failed', name='最新构建失败', description='每个应用的最新构建失败，下一次构建成功才恢复', threshold=0, forSeconds=0, unit='count'),
]
METRICS = {p['metric'] for p in PRESETS}


def _schema(conn):
    statements = [
        '''CREATE TABLE IF NOT EXISTS alert_rules(id TEXT PRIMARY KEY,name TEXT NOT NULL,metric TEXT NOT NULL,target TEXT NOT NULL,threshold REAL NOT NULL,for_seconds INTEGER NOT NULL,severity TEXT NOT NULL,enabled INTEGER NOT NULL,repeat_seconds INTEGER NOT NULL,no_data TEXT NOT NULL,silenced_until REAL NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS alert_channels(id TEXT PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,app_id TEXT NOT NULL,app_secret TEXT NOT NULL,chat_id TEXT NOT NULL,enabled INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS alert_rule_channels(rule_id TEXT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,channel_id TEXT NOT NULL REFERENCES alert_channels(id) ON DELETE CASCADE,PRIMARY KEY(rule_id,channel_id))''',
        '''CREATE TABLE IF NOT EXISTS alert_incidents(id INTEGER PRIMARY KEY AUTOINCREMENT,rule_id TEXT NOT NULL,rule_name TEXT NOT NULL,target TEXT NOT NULL,metric TEXT NOT NULL,severity TEXT NOT NULL,status TEXT NOT NULL,value REAL,no_data INTEGER NOT NULL DEFAULT 0,started_at REAL NOT NULL,fired_at REAL,resolved_at REAL,updated_at REAL NOT NULL,acknowledged_at REAL,last_notified_at REAL)''',
        "CREATE UNIQUE INDEX IF NOT EXISTS alert_active_target ON alert_incidents(rule_id,target) WHERE status IN ('pending','firing')",
        'CREATE INDEX IF NOT EXISTS alert_incidents_status ON alert_incidents(status,id DESC)',
        '''CREATE TABLE IF NOT EXISTS alert_events(id INTEGER PRIMARY KEY AUTOINCREMENT,incident_id INTEGER NOT NULL REFERENCES alert_incidents(id) ON DELETE CASCADE,kind TEXT NOT NULL,at REAL NOT NULL,detail TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS alert_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL,incident_id INTEGER,kind TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL,lease_until REAL,lease_token TEXT,last_error TEXT,created_at REAL NOT NULL,sent_at REAL)''',
        'CREATE INDEX IF NOT EXISTS alert_outbox_due ON alert_outbox(status,next_attempt_at,lease_until)',
        'CREATE TABLE IF NOT EXISTS alert_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    ]
    for sql in statements:
        conn.execute(sql)
    columns={r[1] for r in conn.execute('PRAGMA table_info(alert_channels)')}
    for name in ('app_id','app_secret','chat_id'):
        if name not in columns:
            conn.execute(f"ALTER TABLE alert_channels ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def _id(prefix):
    return prefix + secrets.token_hex(8)


def _now(now):
    return time.time() if now is None else float(now)


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def _bounded(value, name, low, high):
    result = _num(value)
    if result is None or not low <= result <= high:
        raise LumaError(f'{name} must be between {low} and {high}')
    return result


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LumaError(f'{name} must be non-empty text up to {limit} characters')
    return value.strip()


def _rule(conn, row):
    return dict(id=row['id'], name=row['name'], metric=row['metric'], target=row['target'], threshold=row['threshold'],
                forSeconds=row['for_seconds'], severity=row['severity'], enabled=bool(row['enabled']),
                repeatSeconds=row['repeat_seconds'], noData=row['no_data'], silencedUntil=row['silenced_until'],
                channelIds=[r[0] for r in conn.execute('SELECT channel_id FROM alert_rule_channels WHERE rule_id=? ORDER BY channel_id', (row['id'],))])


def _channel(row):
    return dict(id=row['id'], name=row['name'], type=row['type'], enabled=bool(row['enabled']),
                appId=row['app_id'],chatId=row['chat_id'],appSecretConfigured=bool(row['app_secret']))


def _incident(row):
    mapping = {'rule_id':'ruleId','rule_name':'ruleName','no_data':'noData','started_at':'startedAt','fired_at':'firedAt','resolved_at':'resolvedAt','updated_at':'updatedAt','acknowledged_at':'acknowledgedAt','last_notified_at':'lastNotifiedAt'}
    return {mapping.get(k,k): (bool(v) if k == 'no_data' else v) for k,v in dict(row).items()}


def _delivery(row):
    mapping = {'channel_id':'channelId','incident_id':'incidentId','next_attempt_at':'nextAttemptAt','last_error':'lastError','created_at':'createdAt','sent_at':'sentAt'}
    return {mapping.get(k,k):v for k,v in dict(row).items() if k not in {'text','lease_until','lease_token'}}


def _event(conn, incident, kind, now, detail=''):
    conn.execute('INSERT INTO alert_events(incident_id,kind,at,detail) VALUES(?,?,?,?)',(incident,kind,now,detail))


def _setting(conn, key, default='0'):
    row = conn.execute('SELECT value FROM alert_settings WHERE key=?',(key,)).fetchone()
    return row[0] if row else default


def save_rule(body, *, now=None):
    now = _now(now)
    with database.transaction(immediate=True) as conn:
        _schema(conn)
        identifier = _text(body['id'],'id',128) if body.get('id') is not None else _id('rule_')
        existing = conn.execute('SELECT * FROM alert_rules WHERE id=?',(identifier,)).fetchone()
        data = {**(_rule(conn,existing) if existing else dict(enabled=True,target='*',repeatSeconds=3600,noData='keep',silencedUntil=0,channelIds=[],severity='warning',forSeconds=300)),**body}
        name = _text(data.get('name'),'name')
        metric = data.get('metric')
        if metric not in METRICS:
            raise LumaError('unsupported alert metric')
        if data.get('severity') not in ('warning','critical') or data.get('noData') not in ('keep','alert'):
            raise LumaError('invalid severity or noData policy')
        if not isinstance(data.get('enabled'), bool):
            raise LumaError('enabled must be a boolean')
        target = _text(data.get('target'),'target',300)
        threshold = _bounded(data.get('threshold'),'threshold',0,31536000)
        duration = int(_bounded(data.get('forSeconds'),'forSeconds',0,86400))
        repeat = int(_bounded(data.get('repeatSeconds'),'repeatSeconds',60,604800))
        silence = _bounded(data.get('silencedUntil'),'silencedUntil',0,now+604800)
        channels = data.get('channelIds')
        if not isinstance(channels,list) or len(channels)>20 or any(not isinstance(c,str) for c in channels):
            raise LumaError('channelIds must be a list of channel IDs')
        for channel in channels:
            if not conn.execute('SELECT 1 FROM alert_channels WHERE id=?',(channel,)).fetchone():
                raise LumaError('notification channel not found')
        if existing and any(data.get(k) != _rule(conn,existing).get(k) for k in ('metric','target','threshold','forSeconds','noData','enabled')):
            _close_rule(conn,identifier,now,'rule_changed')
        conn.execute('INSERT OR REPLACE INTO alert_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (identifier,name,metric,target,threshold,duration,data['severity'],int(data['enabled']),repeat,data['noData'],silence,existing['created_at'] if existing else now,now))
        conn.execute('DELETE FROM alert_rule_channels WHERE rule_id=?',(identifier,))
        conn.executemany('INSERT INTO alert_rule_channels VALUES(?,?)',[(identifier,c) for c in set(channels)])
        return _rule(conn,conn.execute('SELECT * FROM alert_rules WHERE id=?',(identifier,)).fetchone())


def save_channel(body, *, now=None):
    now = _now(now)
    with database.transaction(immediate=True) as conn:
        _schema(conn)
        identifier = _text(body['id'],'id',128) if body.get('id') is not None else _id('channel_')
        old = conn.execute('SELECT * FROM alert_channels WHERE id=?',(identifier,)).fetchone()
        name = _text(body.get('name',old['name'] if old else None),'name')
        if body.get('type','feishu') != 'feishu':
            raise LumaError('only Feishu notification channels are supported')
        app_id = body.get('appId',old['app_id'] if old else '')
        app_secret = body.get('appSecret',old['app_secret'] if old else '')
        chat_id = body.get('chatId',old['chat_id'] if old else '')
        validate_credentials(app_id,app_secret,chat_id)
        enabled = body.get('enabled',bool(old['enabled']) if old else True)
        if not isinstance(enabled,bool):
            raise LumaError('enabled must be a boolean')
        columns=['id','name','type','app_id','app_secret','chat_id','enabled','created_at','updated_at']
        values=[identifier,name,'feishu',app_id,app_secret,chat_id,int(enabled),old['created_at'] if old else now,now]
        legacy={r[1] for r in conn.execute('PRAGMA table_info(alert_channels)')}
        if 'webhook' in legacy:
            columns.extend(['webhook','secret']);values.extend(['',''])
        updates='name=excluded.name,app_id=excluded.app_id,app_secret=excluded.app_secret,chat_id=excluded.chat_id,enabled=excluded.enabled,updated_at=excluded.updated_at'
        if 'webhook' in legacy: updates+=",webhook='',secret=''"
        conn.execute('INSERT INTO alert_channels('+','.join(columns)+') VALUES('+','.join('?' for _ in values)+') ON CONFLICT(id) DO UPDATE SET '+updates,values)
        return _channel(conn.execute('SELECT * FROM alert_channels WHERE id=?',(identifier,)).fetchone())


def _close_rule(conn,identifier,now,reason):
    for row in conn.execute("SELECT id FROM alert_incidents WHERE rule_id=? AND status IN ('pending','firing')",(identifier,)).fetchall():
        conn.execute("UPDATE alert_incidents SET status='closed',resolved_at=?,updated_at=? WHERE id=?",(now,now,row['id']))
        _event(conn,row['id'],reason,now,'Rule changed or removed; closure is not evidence of metric recovery')
        conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Rule changed or removed' WHERE incident_id=? AND status IN ('pending','retry')",(row['id'],))


def _enqueue(conn,channel,incident,kind,text,now):
    return conn.execute("INSERT INTO alert_outbox(channel_id,incident_id,kind,text,status,next_attempt_at,created_at) VALUES(?,?,?,?,'pending',?,?)",(channel,incident,kind,text,now,now)).lastrowid


def _notify(conn,rule,incident,kind,now,cluster):
    silence = max(float(_setting(conn,'silenced_until')),rule['silenced_until'])
    if now < silence and kind != 'resolved':
        _event(conn,incident['id'],'notification_silenced',now,kind)
        return False
    channels = conn.execute('SELECT c.id FROM alert_channels c JOIN alert_rule_channels rc ON rc.channel_id=c.id WHERE rc.rule_id=? AND c.enabled=1',(rule['id'],)).fetchall()
    if not channels:
        return False
    label = {'firing':'告警触发','resolved':'告警恢复','reminder':'告警持续'}[kind]
    value = '无新鲜数据' if incident['no_data'] else f"{incident['value']:g}"
    text = f"Luma {label} · {rule['name']}\n集群：{cluster}\n对象：{incident['target']}\n级别：{rule['severity']}\n当前值：{value}；阈值：{rule['threshold']:g}\n事件编号：{incident['id']}\n请在 Dashboard → 可观测性 → 告警中心查看。"
    for channel in channels:
        if kind == 'resolved' and not conn.execute("SELECT 1 FROM alert_outbox WHERE incident_id=? AND channel_id=? AND kind IN ('firing','reminder') AND status IN ('sent','sending')",(incident['id'],channel['id'])).fetchone():
            continue
        delivery = _enqueue(conn,channel['id'],incident['id'],kind,text,now)
        if now < silence:
            conn.execute('UPDATE alert_outbox SET next_attempt_at=? WHERE id=?',(silence,delivery))
    conn.execute('UPDATE alert_incidents SET last_notified_at=? WHERE id=?',(now,incident['id']))
    return True


def _observations(state,metric,now):
    result = {}
    if metric.startswith('node.'):
        keys = {'node.cpu':'cpuPercent','node.memory':'memoryUsedPercent','node.disk':'diskUsedPercent','node.inode':'inodesUsedPercent'}
        for target,node in (state.get('nodes') or {}).items():
            if not isinstance(node,dict): continue
            agent = node.get('agent') or {}
            seen = _num(agent.get('lastSeen'))
            if metric == 'node.offline':
                result[target] = max(0,now-seen) if seen else None
            else:
                sampled = _num(agent.get('metricsCollectedAt',seen))
                fresh = seen and sampled and 0<=now-seen<=120 and 0<=now-sampled<=120
                result[target] = _num((agent.get('metrics') or {}).get(keys[metric])) if fresh else None
                if metric == 'node.cpu' and agent.get('os') == 'darwin': result[target] = None
    elif metric == 'task.queue_age':
        if '_alertQueueAge' in state:
            return state['_alertQueueAge']
        for collection,kind in [('agentTasks','agent'),('builderTasks','builder'),('buildRuns','build')]:
            records = (state.get(collection) or {}).values()
            times = [_num(x.get('createdAt')) for x in records if isinstance(x,dict) and x.get('status')=='queued']
            valid = [t for t in times if t and t<=now]
            result[kind] = now-min(valid) if valid else (None if times else 0)
    elif metric == 'build.failed':
        latest = {}
        for item in (state.get('buildRuns') or {}).values():
            if not isinstance(item,dict): continue
            request = item.get('request') if isinstance(item.get('request'),dict) else {}
            target = str(item.get('applicationRef') or item.get('slug') or request.get('name') or request.get('application') or item.get('projectKey') or item.get('app') or item.get('name') or item.get('service') or item.get('id') or '')
            when = _num(item.get('createdAt')) or 0
            if target and (target not in latest or when > latest[target][0]): latest[target] = (when,item)
        for target,(_,item) in latest.items():
            result[target] = 1 if item.get('status') in ('failed','error') else (0 if item.get('status') in ('succeeded','success','completed') else None)
    return result


def tick(state: dict[str,Any], *, now=None, services=None):
    """Evaluate once; no network. Call every 15 seconds independently of browser state."""
    now = _now(now)
    with database.transaction(immediate=True) as conn:
        _schema(conn)
        previous = float(_setting(conn,'last_evaluated_at'))
        rules = conn.execute('SELECT * FROM alert_rules WHERE enabled=1').fetchall()
        for rule in rules:
            observations = _observations(state,rule['metric'],now)
            if rule['target'] != '*': observations = {rule['target']:observations.get(rule['target'])}
            active = {r['target']:r for r in conn.execute("SELECT * FROM alert_incidents WHERE rule_id=? AND status IN ('pending','firing')",(rule['id'],))}
            # Missing targets cannot be silently reported recovered.
            observations.update({target:None for target in active if target not in observations})
            for target,value in observations.items():
                row = active.get(target)
                no_data = value is None
                if no_data and rule['no_data']=='keep':
                    if row:
                        # Missing samples break the continuous pending duration, but retain firing state.
                        conn.execute('UPDATE alert_incidents SET no_data=1,updated_at=?,started_at=CASE WHEN status=\'pending\' THEN ? ELSE started_at END WHERE id=?',(now,now,row['id']))
                    continue
                breached = no_data or value > rule['threshold']
                if breached:
                    if row is None:
                        identifier = conn.execute("INSERT INTO alert_incidents(rule_id,rule_name,target,metric,severity,status,value,no_data,started_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",(rule['id'],rule['name'],target,rule['metric'],rule['severity'],value,int(no_data),now,now)).lastrowid
                        _event(conn,identifier,'pending',now)
                    else:
                        identifier = row['id']
                        # A Manager pause cannot count as continuously observed threshold breach.
                        restart = (previous and now-previous>60) or (row['no_data'] and rule['no_data']=='keep')
                        start = now if row['status']=='pending' and restart else row['started_at']
                        conn.execute('UPDATE alert_incidents SET value=?,no_data=?,updated_at=?,started_at=? WHERE id=?',(value,int(no_data),now,start,identifier))
                    current = conn.execute('SELECT * FROM alert_incidents WHERE id=?',(identifier,)).fetchone()
                    if current['status']=='pending' and now-current['started_at']>=rule['for_seconds']:
                        conn.execute("UPDATE alert_incidents SET status='firing',fired_at=? WHERE id=?",(now,identifier))
                        _event(conn,identifier,'firing',now)
                        _notify(conn,rule,current,'firing',now,str(state.get('clusterId') or ''))
                    elif current['status']=='firing' and not current['acknowledged_at'] and (current['last_notified_at'] is None or now-current['last_notified_at']>=rule['repeat_seconds']):
                        # Silence expiry sends an active alert even if initial firing was suppressed.
                        if now>=max(float(_setting(conn,'silenced_until')),rule['silenced_until']):
                            _notify(conn,rule,current,'reminder' if current['last_notified_at'] else 'firing',now,str(state.get('clusterId') or ''))
                elif row:
                    conn.execute("UPDATE alert_incidents SET status='resolved',value=?,no_data=0,resolved_at=?,updated_at=? WHERE id=?",(value,now,now,row['id']))
                    _event(conn,row['id'],'resolved',now)
                    # Do not emit an obsolete firing after recovery, e.g. an outage during maintenance.
                    conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Incident recovered before delivery' WHERE incident_id=? AND kind IN ('firing','reminder') AND status IN ('pending','retry')",(row['id'],))
                    if row['status']=='firing' and row['last_notified_at'] is not None:
                        current = conn.execute('SELECT * FROM alert_incidents WHERE id=?',(row['id'],)).fetchone()
                        _notify(conn,rule,current,'resolved',now,str(state.get('clusterId') or ''))
        conn.execute("INSERT INTO alert_settings VALUES('last_evaluated_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now),))
    return {'evaluatedAt':now,'rules':len(rules)}


def deliver_pending(*, now=None, transport: Callable = send_feishu, limit=10):
    """Claim durable leases, send outside transaction, retry up to eight attempts."""
    now = _now(now)
    count = 0
    for _ in range(min(max(int(limit),0),100)):
        lease = secrets.token_hex(16)
        with database.transaction(immediate=True) as conn:
            _schema(conn)
            row = conn.execute("SELECT * FROM alert_outbox WHERE ((status IN ('pending','retry') AND next_attempt_at<=?) OR (status='sending' AND lease_until<=?)) ORDER BY id LIMIT 1",(now,now)).fetchone()
            if not row: break
            if row['attempts']>=8:
                conn.execute("UPDATE alert_outbox SET status='failed',last_error='Delivery attempts exhausted after interrupted sends',lease_until=NULL,lease_token=NULL WHERE id=?",(row['id'],))
                continue
            if row['kind'] in ('firing','reminder'):
                current = conn.execute('SELECT i.status,i.acknowledged_at,r.enabled FROM alert_incidents i LEFT JOIN alert_rules r ON r.id=i.rule_id WHERE i.id=?',(row['incident_id'],)).fetchone()
                if not current or current['status']!='firing' or not current['enabled'] or (row['kind']=='reminder' and current['acknowledged_at'] is not None):
                    conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Incident no longer firing or rule disabled',lease_until=NULL,lease_token=NULL WHERE id=?",(row['id'],))
                    continue
            channel = conn.execute('SELECT * FROM alert_channels WHERE id=?',(row['channel_id'],)).fetchone()
            if not channel or not channel['enabled']:
                conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Channel unavailable or disabled' WHERE id=?",(row['id'],))
                continue
            if row['kind'] != 'test':
                silence = float(_setting(conn,'silenced_until'))
                incident = conn.execute('SELECT rule_id FROM alert_incidents WHERE id=?',(row['incident_id'],)).fetchone()
                rule = conn.execute('SELECT silenced_until FROM alert_rules WHERE id=?',(incident['rule_id'],)).fetchone() if incident else None
                silence = max(silence,rule[0] if rule else 0)
                if silence > now:
                    conn.execute("UPDATE alert_outbox SET status='retry',next_attempt_at=?,lease_until=NULL,lease_token=NULL WHERE id=?",(silence,row['id']))
                    continue
            # Keep order within an incident/channel across a retry, especially firing -> recovery.
            earlier = conn.execute("SELECT 1 FROM alert_outbox WHERE id<? AND channel_id=? AND incident_id IS ? AND status IN ('pending','retry','sending')",(row['id'],row['channel_id'],row['incident_id'])).fetchone()
            if earlier:
                conn.execute('UPDATE alert_outbox SET next_attempt_at=? WHERE id=?',(now+15,row['id']))
                continue
            conn.execute("UPDATE alert_outbox SET status='sending',attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?",(now+60,lease,row['id']))
            app_id,app_secret,chat_id,text = channel['app_id'],channel['app_secret'],channel['chat_id'],row['text']
            namespace=_setting(conn,'notification_namespace','')
            if not namespace:
                namespace=secrets.token_hex(16)
                conn.execute("INSERT INTO alert_settings VALUES('notification_namespace',?)",(namespace,))
            delivery_uuid=hashlib.sha256(f"{namespace}:{row['id']}".encode()).hexdigest()[:40]
        error = None
        retryable = True
        try:
            transport(app_id,app_secret,chat_id,text,delivery_uuid)
        except FeishuError as exc:
            error,retryable = str(exc),exc.retryable
        except Exception:
            # Even HTTP/server errors can contain echoed secrets. Persist a fixed diagnostic only.
            error = 'Notification failed; check Feishu application credentials, group access, network, and rate limits'
        with database.transaction(immediate=True) as conn:
            _schema(conn)
            claimed = conn.execute('SELECT * FROM alert_outbox WHERE id=? AND lease_token=?',(row['id'],lease)).fetchone()
            if not claimed: continue
            status = ('failed' if claimed['attempts']>=8 or not retryable else 'retry') if error else 'sent'
            if error and claimed['kind'] in ('firing','reminder'):
                current = conn.execute('SELECT status,acknowledged_at FROM alert_incidents WHERE id=?',(claimed['incident_id'],)).fetchone()
                if not current or current['status'] in ('resolved','closed') or (claimed['kind']=='reminder' and current['acknowledged_at'] is not None):
                    status,error = 'cancelled','Incident closed while notification was in flight'
            conn.execute('UPDATE alert_outbox SET status=?,last_error=?,next_attempt_at=?,sent_at=?,lease_until=NULL,lease_token=NULL WHERE id=? AND lease_token=?',
                         (status,error,now+min(3600,15*2**min(claimed['attempts']-1,8)),None if error else now,row['id'],lease))
        count += 1
    return {'processed':count}


def load_evaluation_state(*, now=None):
    """Read current samples and SQL summaries without materializing historical event bodies."""
    now = _now(now)
    with database.transaction(immediate=False) as conn:
        database.ensure_initialized(conn)
        row = conn.execute("SELECT payload FROM control_config WHERE key='clusterId'").fetchone()
        result = {'clusterId':json.loads(row[0]) if row else '', 'nodes':{}, 'buildRuns':{}, '_alertQueueAge':{}}
        for row in conn.execute("SELECT id,payload FROM control_entities WHERE kind='nodes'"):
            result['nodes'][row['id']] = json.loads(row['payload'])
        for collection,kind in [('agentTasks','agent'),('builderTasks','builder'),('buildRuns','build')]:
            row = conn.execute("SELECT COUNT(*) n,MIN(CASE WHEN created_at>0 AND created_at<=? THEN created_at END) oldest FROM control_entities WHERE kind=? AND status='queued'",(now,collection)).fetchone()
            result['_alertQueueAge'][kind] = now-row['oldest'] if row['oldest'] is not None else (None if row['n'] else 0)
        # Window query returns one small summary per application. Detailed events remain separate.
        rows = conn.execute("""SELECT id,payload FROM (SELECT id,payload,ROW_NUMBER() OVER (PARTITION BY CASE WHEN app='' THEN id ELSE app END ORDER BY created_at DESC,id DESC) rank FROM control_entities WHERE kind='buildRuns') WHERE rank=1""")
        for row in rows:
            result['buildRuns'][row['id']] = json.loads(row['payload'])
        return result


def retention_plan(conn, *, cutoff):
    """Reviewable bounded plan; caller owns transaction and retention policy (e.g. 90 days)."""
    _schema(conn)
    cutoff = _bounded(cutoff,'cutoff',0,253402300799)
    deliveries = conn.execute("""SELECT id,length(text) size FROM alert_outbox o
        WHERE created_at<? AND status IN ('sent','failed','cancelled')
        AND (incident_id IS NULL OR NOT EXISTS(SELECT 1 FROM alert_incidents i WHERE i.id=o.incident_id AND i.status IN ('pending','firing')))
        ORDER BY id LIMIT 1001""",(cutoff,)).fetchall()
    incidents = conn.execute("""SELECT id FROM alert_incidents i WHERE status IN ('resolved','closed') AND resolved_at<?
        AND NOT EXISTS(SELECT 1 FROM alert_outbox o WHERE o.incident_id=i.id AND (o.status NOT IN ('sent','failed','cancelled') OR o.created_at>=?))
        ORDER BY id LIMIT 1001""",(cutoff,cutoff)).fetchall()
    identifiers=[r['id'] for r in incidents[:1000]]
    events_count=0
    if identifiers:
        events_count=conn.execute('SELECT COUNT(*) FROM alert_events WHERE incident_id IN ('+','.join('?' for _ in identifiers)+')',identifiers).fetchone()[0]
    return dict(cutoff=cutoff,incidentIds=identifiers,deliveryIds=[r['id'] for r in deliveries[:1000]],eventsCount=events_count,
                estimatedBytes=sum(r['size'] or 0 for r in deliveries[:1000]),hasMore=len(deliveries)>1000 or len(incidents)>1000)


def prune(conn, plan):
    """Revalidate every reviewed ID; never delete active incidents or outstanding notifications."""
    _schema(conn)
    cutoff=_bounded(plan.get('cutoff'),'cutoff',0,253402300799)
    delivery_ids=plan.get('deliveryIds',[])
    incident_ids=plan.get('incidentIds',[])
    if not isinstance(delivery_ids,list) or not isinstance(incident_ids,list) or max(len(delivery_ids),len(incident_ids))>1000:
        raise LumaError('invalid alert retention plan')
    deliveries=incidents=0
    for identifier in delivery_ids:
        deliveries+=conn.execute("""DELETE FROM alert_outbox WHERE id=? AND created_at<? AND status IN ('sent','failed','cancelled')
            AND (incident_id IS NULL OR NOT EXISTS(SELECT 1 FROM alert_incidents i WHERE i.id=alert_outbox.incident_id AND i.status IN ('pending','firing')))""",(identifier,cutoff)).rowcount
    for identifier in incident_ids:
        incidents+=conn.execute("""DELETE FROM alert_incidents WHERE id=? AND status IN ('resolved','closed') AND resolved_at<?
            AND NOT EXISTS(SELECT 1 FROM alert_outbox o WHERE o.incident_id=alert_incidents.id)""",(identifier,cutoff)).rowcount
    return {'incidentsDeleted':incidents,'deliveriesDeleted':deliveries}


def dispatch(method: str, resource: str, body=None, query=None):
    """Authenticated server adapter: resource excludes /v1/alerting/."""
    body,query = body or {},query or {}
    parts = resource.strip('/').split('/')
    name = parts[0]
    identifier = parts[1] if len(parts)>1 else None
    now = time.time()
    if method=='POST' and name=='rules' and not identifier: return save_rule(body)
    if method=='POST' and name=='channels' and not identifier: return save_channel(body)
    with database.transaction(immediate=True) as conn:
        _schema(conn)
        if method=='GET' and name=='presets': return {'items':PRESETS}
        if method=='GET' and name=='overview':
            counts = {s:0 for s in ('pending','firing','resolved','closed')}
            counts.update({r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM alert_incidents GROUP BY status')})
            return dict(counts=counts,lastEvaluatedAt=float(_setting(conn,'last_evaluated_at')) or None,silencedUntil=float(_setting(conn,'silenced_until')),enabledRules=conn.execute('SELECT COUNT(*) FROM alert_rules WHERE enabled=1').fetchone()[0],channels=conn.execute('SELECT COUNT(*) FROM alert_channels WHERE enabled=1').fetchone()[0])
        if method=='POST' and name=='silence':
            seconds = _bounded(body.get('seconds'),'seconds',0,604800)
            until = now+seconds if seconds else 0
            conn.execute("INSERT INTO alert_settings VALUES('silenced_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(until),))
            return {'silencedUntil':until}
        if name in ('rules','channels') and method=='GET':
            return {'items':[(_rule(conn,r) if name=='rules' else _channel(r)) for r in conn.execute('SELECT * FROM alert_'+name+' ORDER BY created_at DESC')]}
        if method=='DELETE' and name in ('rules','channels') and identifier:
            if name=='rules': _close_rule(conn,identifier,now,'rule_deleted')
            changed = conn.execute('DELETE FROM alert_'+name+' WHERE id=?',(identifier,)).rowcount
            if not changed: raise LumaError('alert item not found')
            return {'deleted':True}
        if method=='POST' and name=='channels' and identifier and parts[-1]=='test':
            channel = conn.execute('SELECT * FROM alert_channels WHERE id=?',(identifier,)).fetchone()
            if not channel or not channel['enabled']: raise LumaError('enabled notification channel not found')
            last = conn.execute("SELECT created_at FROM alert_outbox WHERE channel_id=? AND kind='test' ORDER BY id DESC LIMIT 1",(identifier,)).fetchone()
            if last and now-last[0]<30: raise LumaError('wait 30 seconds between channel tests')
            delivery = _enqueue(conn,identifier,None,'test','Luma 通知测试：飞书告警渠道连接验证。',now)
            return {'delivery':_delivery(conn.execute('SELECT * FROM alert_outbox WHERE id=?',(delivery,)).fetchone())}
        if name=='incidents' and identifier:
            row = conn.execute('SELECT * FROM alert_incidents WHERE id=?',(identifier,)).fetchone()
            if not row: raise LumaError('alert incident not found')
            if method=='POST' and parts[-1]=='ack':
                conn.execute('UPDATE alert_incidents SET acknowledged_at=? WHERE id=?',(now,identifier))
                conn.execute("UPDATE alert_outbox SET status='cancelled',last_error='Incident acknowledged' WHERE incident_id=? AND kind='reminder' AND status IN ('pending','retry')",(identifier,))
                _event(conn,row['id'],'acknowledged',now)
                return {'incident':_incident(conn.execute('SELECT * FROM alert_incidents WHERE id=?',(identifier,)).fetchone())}
            if method=='GET':
                return {'incident':_incident(row),'events':[dict(r) for r in conn.execute('SELECT id,kind,at,detail FROM alert_events WHERE incident_id=? ORDER BY id',(identifier,))]}
        if method=='GET' and name in ('incidents','deliveries'):
            limit = int(_bounded(query.get('limit',50),'limit',1,200))
            try:
                cursor = int(query.get('cursor',9223372036854775807))
            except (TypeError,ValueError):
                raise LumaError('invalid cursor') from None
            if not 1<=cursor<=9223372036854775807: raise LumaError('invalid cursor')
            table = 'alert_incidents' if name=='incidents' else 'alert_outbox'
            where,args = 'id<?',[cursor]
            if query.get('status'):
                where+=' AND status=?';args.append(str(query['status']))
            rows = conn.execute(f'SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ?',(*args,limit+1)).fetchall()
            return {'items':[(_incident(r) if name=='incidents' else _delivery(r)) for r in rows[:limit]],'nextCursor':str(rows[limit-1]['id']) if len(rows)>limit else None}
    raise LumaError('unknown alerting endpoint')
