"""Authenticated Feishu card callbacks for expiring source notification silences.

Protocol: larksuite/oapi-sdk-python event/dispatcher_handler.py,
core/utils/decryptor.py and event/callback/model/p2_card_action_trigger.py.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ..errors import DatabaseBusyError, LumaError
from . import alert_source_silences, database

MAX_BODY = 65536
ACTION = 'silence_source'


class CallbackDenied(LumaError):
    def __init__(self):
        super().__init__('Invalid Feishu card callback')


def save_config(conn, app_id, body):
    """Keep callback credentials at application scope, shared by its group channels."""
    enabled = body['cardActionsEnabled']
    if not isinstance(enabled, bool):
        raise LumaError('cardActionsEnabled must be a boolean')
    if not enabled:
        conn.execute('DELETE FROM alert_feishu_callbacks WHERE app_id=?', (app_id,))
        return
    previous = conn.execute('SELECT * FROM alert_feishu_callbacks WHERE app_id=?', (app_id,)).fetchone()
    values = []
    for field, column in (('callbackVerificationToken', 'verification_token'), ('callbackEncryptKey', 'encrypt_key')):
        value = body.get(field) or (previous[column] if previous else '')
        if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(c.isspace() for c in value):
            raise LumaError(field + ' is required for Feishu card callbacks')
        values.append(value)
    conn.execute('''INSERT INTO alert_feishu_callbacks VALUES(?,?,?) ON CONFLICT(app_id)
        DO UPDATE SET verification_token=excluded.verification_token,encrypt_key=excluded.encrypt_key''', (app_id, *values))


def _source_elements(sources, delivery, token, silences=None):
    """Show exact scopes beside the duration choices on the shared group card."""
    elements = []
    for item in sources[:3]:
        label = {'task': '任务', 'service': '服务', 'target': '告警对象'}[item['kind']]
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': f"{label}：{item['name']}"}})
        until = (silences or {}).get(item['key'])
        if until:
            stamp = datetime.fromtimestamp(until, timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
            elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': f'此来源已静默至 {stamp}（UTC+08:00），其他来源照常通知。'}})
        else:
            elements.append({'tag': 'action', 'actions': [
                {'tag': 'button', 'text': {'tag': 'plain_text', 'content': f'静默 {hours} 小时'}, 'type': 'default',
                 'value': {'action': ACTION, 'deliveryId': delivery, 'sourceKey': item['key'], 'seconds': hours * 3600, 'token': token}}
                for hours in (1, 6, 24)]})
    if len(sources) > 3:
        elements.append({'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': '更多来源可在告警详情中限时静默。'}]})
    return elements


def attach_actions(conn, channel_id, delivery, payload):
    """Issue an opaque action capability tied to one persisted delivery and its sources."""
    configured = conn.execute('''SELECT 1 FROM alert_channels c JOIN alert_feishu_callbacks f
        ON f.app_id=c.app_id WHERE c.id=? AND c.enabled=1''', (channel_id,)).fetchone()
    if not configured or not payload.get('sources'):
        return payload
    token = secrets.token_hex(32)
    conn.execute('UPDATE alert_outbox SET action_token=? WHERE id=?', (token, delivery))
    card = payload['card']
    card['config']['update_multi'] = True
    start = len(card['elements']) - 1
    payload['actionStart'] = start
    card['elements'][start:start] = _source_elements(payload['sources'], delivery, token)
    return payload


def _request_time(stamp):
    """Feishu callbacks also send Go Time.String(), including its monotonic suffix."""
    if stamp.isdigit() and len(stamp) <= 12:
        return int(stamp)
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d{1,9})? ([+-]\d{4}) '
                         r'[A-Za-z0-9+-]{1,16}(?: m=[+-]\d+(?:\.\d{1,9})?)?', stamp)
    if not match:
        raise ValueError()
    return (datetime.strptime(match[1] + match[3], '%Y-%m-%d %H:%M:%S%z').timestamp()
            + float(match[2] or 0))


def decode_callback(raw, headers, config, now):
    """Verify the challenge token or signed request before exposing action values."""
    try:
        if len(raw) > MAX_BODY:
            raise ValueError()
        payload = json.loads(raw)
        if payload.get('encrypt'):
            encrypted = base64.b64decode(payload['encrypt'], validate=True)
            if len(encrypted) < 32 or len(encrypted) % 16:
                raise ValueError()
            cipher = Cipher(algorithms.AES(hashlib.sha256(config['encrypt_key'].encode()).digest()), modes.CBC(encrypted[:16]))
            decryptor = cipher.decryptor()
            padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            payload = json.loads(unpadder.update(padded) + unpadder.finalize())
        if payload.get('type') == 'url_verification':
            if not hmac.compare_digest(str(payload.get('token', '')).encode(), config['verification_token'].encode()):
                raise ValueError()
            if not isinstance(payload.get('challenge'), str) or len(payload['challenge']) > 512:
                raise ValueError()
            return payload
        header = payload['header']
        stamp, nonce = headers.get('x-lark-request-timestamp', ''), headers.get('x-lark-request-nonce', '')
        if len(stamp) > 128 or abs(now - _request_time(stamp)) > 300 or not 1 <= len(nonce) <= 256:
            raise ValueError()
        expected = hashlib.sha256((stamp + nonce + config['encrypt_key']).encode() + raw).hexdigest()
        if not hmac.compare_digest(expected.encode(), headers.get('x-lark-signature', '').encode()):
            raise ValueError()
        if header.get('token') is not None and not hmac.compare_digest(str(header['token']).encode(), config['verification_token'].encode()):
            raise ValueError()
        if (payload.get('schema') != '2.0' or header.get('event_type') != 'card.action.trigger'
                or header.get('app_id') != config['app_id']):
            raise ValueError()
        return payload
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CallbackDenied() from None


def handle(app_id, raw, headers, *, now=None):
    """Apply authenticated source silences once and return a synchronous card update."""
    from . import alerting
    now = time.time() if now is None else now
    with database.transaction(owner='feishu.card_callback', wait_seconds=1.5) as conn:
        alerting._schema(conn)
        config = conn.execute('SELECT * FROM alert_feishu_callbacks WHERE app_id=?', (app_id,)).fetchone()
        if config is None:
            raise CallbackDenied()
        payload = decode_callback(raw, headers, config, now)
        if payload.get('type') == 'url_verification':
            return {'challenge': payload['challenge']}
        try:
            event = payload['event']
            action = event['action']['value']
            context = event['context']
            operator = event['operator']['open_id']
            event_id = payload['header']['event_id']
            if (event.get('host') != 'im_message' or event['action'].get('tag') != 'button'
                    or action.get('action') != ACTION or not isinstance(action.get('deliveryId'), int)
                    or isinstance(action['deliveryId'], bool)
                    or not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,128}', operator)
                    or not isinstance(event_id, str) or not 1 <= len(event_id) <= 128):
                raise ValueError()
            delivery = conn.execute('''SELECT o.*,c.app_id,c.chat_id,c.enabled FROM alert_outbox o
                JOIN alert_channels c ON c.id=o.channel_id WHERE o.id=?''', (action['deliveryId'],)).fetchone()
            if (not delivery or not delivery['enabled'] or delivery['app_id'] != app_id
                    or delivery['chat_id'] != context.get('open_chat_id')
                    or delivery['kind'] not in ('firing', 'reminder') or delivery['status'] not in ('sent', 'sending')
                    or not delivery['action_token'] or not isinstance(action.get('token'), str)
                    or not hmac.compare_digest(delivery['action_token'].encode(), action['token'].encode())):
                raise ValueError()
            card_payload = json.loads(delivery['text'])
            selected = next(item for item in card_payload['sources'] if item['key'] == action.get('sourceKey'))
        except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
            raise CallbackDenied() from None
        incident = conn.execute('SELECT * FROM alert_incidents WHERE id=?', (delivery['incident_id'],)).fetchone()
        rule = conn.execute('SELECT * FROM alert_rules WHERE id=?', (incident['rule_id'],)).fetchone() if incident else None
        if not rule or not rule['enabled'] or incident['status'] not in ('pending', 'firing'):
            return {'toast': {'type': 'info', 'content': '本次告警已结束，请从最新告警卡片选择静默来源。'}}
        previous = conn.execute('SELECT * FROM alert_card_actions WHERE app_id=? AND event_id=?', (app_id, event_id)).fetchone()
        if previous is None:
            # The card is a durable source snapshot even when this window's top
            # sources changed. Revalidate against its original rule and target.
            details = json.loads(incident['log_details'])
            proof = dict(incident)
            if rule['metric'] == 'app.log_matches':
                proof['log_details'] = json.dumps({**details, 'sources': [selected], 'matchCount': selected['count']})
            silenced = alert_source_silences.silence(conn, rule, proof, selected['key'], action.get('seconds'), now,
                                                    actor='feishu:' + operator)
            conn.execute('INSERT INTO alert_card_actions VALUES(?,?,?,?,?)',
                         (app_id, event_id, delivery['id'], selected['key'], silenced['until']))
        elif previous['delivery_id'] != delivery['id'] or previous['source_key'] != selected['key']:
            raise CallbackDenied()
        silences = {r['source_key']: r['until_at'] for r in conn.execute(
            'SELECT source_key,until_at FROM alert_source_silences WHERE rule_id=? AND target=? AND until_at>?',
            (rule['id'], incident['target'], now))}
        card = copy.deepcopy(card_payload['card'])
        card['elements'] = (card['elements'][:card_payload['actionStart']]
                            + _source_elements(card_payload['sources'], delivery['id'], delivery['action_token'], silences)
                            + card['elements'][-1:])
        return {'toast': {'type': 'success', 'content': '此来源已限时静默，其他来源照常告警。'},
                'card': {'type': 'raw', 'data': card}}


async def endpoint(request):
    """Expose only the signed Feishu protocol, independently of dashboard sessions."""
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                return JSONResponse({'error': 'Callback body too large'}, status_code=413)
        result = await run_in_threadpool(handle, request.path_params['app_id'], bytes(raw), dict(request.headers))
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except CallbackDenied:
        return JSONResponse({'error': 'Invalid Feishu card callback'}, status_code=401)
    except DatabaseBusyError:
        return JSONResponse({'toast': {'type': 'error', 'content': '告警中心繁忙，请稍后重试。'}})
    except LumaError as exc:
        return JSONResponse({'toast': {'type': 'error', 'content': str(exc)}})
