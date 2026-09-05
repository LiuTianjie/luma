"""Feishu application-bot transport with process-local tenant token cache.

Official protocol and SDK schemas:
https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
https://open.feishu.cn/document/server-docs/im-v1/message/create
https://github.com/larksuite/oapi-sdk-python/tree/v2_main/lark_oapi/api/im/v1/model
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from ..errors import LumaError

AUTH_URL = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
MESSAGE_URL = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id'
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


class FeishuError(LumaError):
    """Only fixed, credential-free diagnostic messages are accepted here."""
    def __init__(self, category: str, *, retryable: bool = True):
        messages = {
            'invalid_credentials': 'Feishu App ID or App Secret was rejected; verify application credentials',
            'no_permission': 'Feishu application lacks message permission; enable and publish im:message:send_as_bot',
            'bot_not_in_chat': 'Feishu bot cannot access this chat; add the application bot to the target group and verify Chat ID',
            'invalid_token': 'Feishu tenant access token was rejected; a fresh token will be requested',
            'rate_limited': 'Feishu rate limit reached; delivery will retry with backoff',
            'unavailable': 'Feishu transport unavailable or timed out',
            'invalid_response': 'Feishu returned an invalid response',
            'rejected': 'Feishu rejected the message; verify bot capability, application release and group permissions',
        }
        self.category, self.retryable = category, retryable
        super().__init__(messages[category])


def validate_credentials(app_id: str, app_secret: str, chat_id: str) -> None:
    if not isinstance(app_id,str) or not re.fullmatch(r'cli_[A-Za-z0-9_-]{4,128}',app_id):
        raise LumaError('App ID must be a Feishu application ID starting with cli_')
    if not isinstance(chat_id,str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]{4,128}',chat_id):
        raise LumaError('Chat ID must be a Feishu group ID starting with oc_')
    if not isinstance(app_secret,str) or not 1<=len(app_secret)<=512 or any(c.isspace() for c in app_secret):
        raise LumaError('App Secret is required and must not contain whitespace')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _classify(data: dict, *, auth: bool) -> FeishuError:
    code = data.get('code')
    # Inspect provider messages only for classification; never persist or echo their content.
    message = str(data.get('msg') or '').lower()
    if code in (99991400,99991403,99991429) or 'rate limit' in message or 'too many' in message:
        return FeishuError('rate_limited')
    if code in (99991663,99991664,99991668) or ('token' in message and any(x in message for x in ('invalid','expired'))):
        return FeishuError('invalid_token')
    if 'permission' in message or 'scope' in message or code == 99991672:
        return FeishuError('no_permission',retryable=False)
    if ('bot' in message and ('not in' in message or 'outside' in message)) or 'chat not found' in message or 'chat_id is invalid' in message:
        return FeishuError('bot_not_in_chat',retryable=False)
    if auth:
        return FeishuError('invalid_credentials',retryable=False)
    return FeishuError('rejected',retryable=False)


def _post(url: str, data: dict, *, token: str = '', deadline: float) -> dict:
    if url not in (AUTH_URL,MESSAGE_URL):
        raise FeishuError('rejected',retryable=False)
    remaining = deadline-time.monotonic()
    if remaining<=0:
        raise FeishuError('unavailable')
    headers = {'Content-Type':'application/json; charset=utf-8'}
    if token: headers['Authorization'] = 'Bearer '+token
    request = urllib.request.Request(url,data=json.dumps(data,ensure_ascii=False).encode(),headers=headers,method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect()).open(request,timeout=min(8,remaining)) as response:
            raw=response.read(65537)
        if len(raw)>65536: raise FeishuError('invalid_response')
        result=json.loads(raw)
        if not isinstance(result,dict): raise FeishuError('invalid_response')
        if result.get('code') != 0: raise _classify(result,auth=url==AUTH_URL)
        return result
    except urllib.error.HTTPError as exc:
        # Feishu returns API business codes on non-2xx responses too. Preserve
        # classification (especially expired tokens) without retaining raw text.
        business = None
        try:
            raw = exc.read(65537)
            if len(raw) <= 65536:
                candidate = json.loads(raw)
                if isinstance(candidate, dict) and isinstance(candidate.get('code'), int) and not isinstance(candidate['code'], bool) and candidate['code'] != 0:
                    business = candidate
        except (OSError, ValueError, TypeError):
            pass
        finally:
            exc.close()
        if business is not None:
            raise _classify(business, auth=url==AUTH_URL) from None
        if exc.code==429: raise FeishuError('rate_limited') from None
        if exc.code==401: raise FeishuError('invalid_credentials' if url==AUTH_URL else 'invalid_token',retryable=url!=AUTH_URL) from None
        if exc.code==403: raise FeishuError('no_permission',retryable=False) from None
        if exc.code>=500: raise FeishuError('unavailable') from None
        raise FeishuError('rejected',retryable=False) from None
    except (urllib.error.URLError,TimeoutError,OSError):
        raise FeishuError('unavailable') from None
    except (ValueError,TypeError):
        raise FeishuError('invalid_response') from None


def _cache_key(app_id: str,app_secret: str) -> str:
    return hashlib.sha256((app_id+'\0'+app_secret).encode()).hexdigest()


def _tenant_token(app_id: str,app_secret: str,*,deadline: float) -> str:
    key=_cache_key(app_id,app_secret)
    with _TOKEN_LOCK:
        cached=_TOKEN_CACHE.get(key)
        if cached and cached[1]>time.monotonic(): return cached[0]
        result=_post(AUTH_URL,{'app_id':app_id,'app_secret':app_secret},deadline=deadline)
        token,expire=result.get('tenant_access_token'),result.get('expire')
        if not isinstance(token,str) or not token or len(token)>4096 or any(c.isspace() for c in token) or not isinstance(expire,(int,float)) or isinstance(expire,bool) or not 0<expire<=86400:
            raise FeishuError('invalid_response')
        # Refresh early; use monotonic time so wall-clock adjustments cannot extend credentials.
        ttl=max(0,expire-min(600,expire*0.1))
        if len(_TOKEN_CACHE)>=128:
            _TOKEN_CACHE.pop(min(_TOKEN_CACHE,key=lambda k:_TOKEN_CACHE[k][1]))
        _TOKEN_CACHE[key]=(token,time.monotonic()+ttl)
        return token


def send_feishu(app_id: str, app_secret: str, chat_id: str, text: str, delivery_uuid: str) -> None:
    """Send to one configured group. Stable uuid is retained across outbox retries."""
    validate_credentials(app_id,app_secret,chat_id)
    deadline=time.monotonic()+16
    body={'receive_id':chat_id,'msg_type':'text','content':json.dumps({'text':text[:16000]},ensure_ascii=False),'uuid':delivery_uuid}
    token=_tenant_token(app_id,app_secret,deadline=deadline)
    try:
        _post(MESSAGE_URL,body,token=token,deadline=deadline)
    except FeishuError as exc:
        if exc.category!='invalid_token': raise
        key=_cache_key(app_id,app_secret)
        with _TOKEN_LOCK:
            cached=_TOKEN_CACHE.get(key)
            if cached and cached[0]==token: _TOKEN_CACHE.pop(key,None)
        # Reacquire once immediately if the existing cache was revoked/expired.
        token=_tenant_token(app_id,app_secret,deadline=deadline)
        _post(MESSAGE_URL,body,token=token,deadline=deadline)
