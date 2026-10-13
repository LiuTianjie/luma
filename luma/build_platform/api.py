from __future__ import annotations

import hmac
import json
import re
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..errors import LumaError
from .blobs import Blobs
from .config import Settings
from .model import Conflict, DIGEST, NotFound, QueueFull, TERMINAL, summary
from .registry import Harbor
from .store import Store


def executor_lease(settings, store, blobs, attempt):
    run = store.get(attempt['run_id'], private=True)
    if run['cancel_requested'] or run['status'] in TERMINAL:
        raise Conflict('build is no longer accepting executor work')
    plan = run['prepared']
    platform = plan['platforms'][attempt['number'] - 1]
    targets = []
    caches = store.caches(plan['recipeHash'])
    for target in plan['targets']:
        if platform not in target['platforms']:
            continue
        repository = settings.registry_host + '/' + target['repository']
        cache_repo = repository + '-cache'
        suffix = attempt['id'] + '-' + platform.replace('/', '-')
        targets.append({**target, 'pushRef': repository + ':' + suffix,
            'cacheRef': cache_repo + ':' + suffix,
            'cacheImports': [ref for ref in caches if ref.startswith(cache_repo + ':') and ref.endswith(platform.replace('/', '-'))]})
    return {'sourceUrl': blobs.url(plan['sourceObject']), 'sourceBytes': plan['sourceBytes'],
        'sourceChecksum': plan['sourceChecksum'], 'proxy': plan.get('proxy') or '', 'platform': platform, 'targets': targets,
        'registryHost': settings.registry_host, 'insecure': settings.registry_url.startswith('http:'),
        'registryAuth': {'username': settings.registry_username, 'password': settings.registry_password},
        'timeout': max(60, min(int(run['body'].get('buildTimeout') or 7200), 86400))}


def detail(store, blobs, run_id, query):
    run = store.get(run_id)
    from .model import decode_cursor, encode_cursor
    limit = int(query.get('limit') or 50)
    if not 1 <= limit <= 100:
        raise Conflict('limit must be between 1 and 100')
    after, through = int(query.get('after', -1)), run['next_event'] - 1
    if query.get('cursor'):
        if 'after' in query:
            raise Conflict('after cannot be combined with cursor')
        mark = decode_cursor(query['cursor'])
        if mark.get('run') != run_id:
            raise Conflict('event cursor belongs to a different build')
        after, through = int(mark['after']), int(mark['through'])
    if after < -1 or (query.get('cursor') and through < after):
        raise Conflict('invalid events cursor')
    events = blobs.events(store, run_id, after, through, limit)
    last = after + len(events)
    more = last < through
    return {'run': {**summary(run), 'events': events}, 'eventsPage': {'limit': limit,
        'resumeAfter': last, 'hasMore': more, 'nextCursor': encode_cursor({'run': run_id, 'after': last, 'through': through}) if more else None}}


def create_app(settings=None, *, store=None, blobs=None, harbor=None):
    settings = settings or Settings.from_env()
    owned = store is None
    store, blobs, harbor = store or Store(settings), blobs or Blobs(settings), harbor or Harbor(settings)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(store.migrate)
        yield
        if owned:
            store.close()

    def dispatch(method, path, token, body, query):
        executor = re.fullmatch(r'/v1/executors/(bld2-[a-f0-9]+-a\d+)/(lease|logs|receipt)', path)
        if executor:
            attempt = store.executor_auth(executor[1], token)
            if method == 'GET' and executor[2] == 'lease':
                from .activities import control_request
                from .placement import selected_nodes
                node_id = query.get('nodeId')
                pool = control_request(settings, 'pool', {})['nodes']
                run = store.get(attempt['run_id'], private=True)
                platform = run['prepared']['platforms'][attempt['number'] - 1]
                nodes = selected_nodes(pool, platform, run['body'].get('buildNode', ''))
                if not node_id or node_id not in {node['nodeId'] for node in nodes}:
                    raise Conflict('executor node is no longer in the approved native build-node list')
                return executor_lease(settings, store, blobs, attempt)
            if method == 'POST' and executor[2] == 'logs':
                events, batch = body.get('events'), str(body.get('batchId') or '')
                if not batch.startswith(attempt['id'] + '-') or len(batch) > 160 or not isinstance(events, list) or not 1 <= len(events) <= 100:
                    raise Conflict('invalid log batch')
                cleaned = []
                for event in events:
                    if not isinstance(event, dict):
                        raise Conflict('invalid log event')
                    cleaned.append({key: str(event.get(key) or '')[:8192] for key in ('name', 'status', 'message', 'ts')})
                return blobs.append(store, attempt['run_id'], batch, cleaned)
            if method == 'POST' and executor[2] == 'receipt':
                lease = executor_lease(settings, store, blobs, attempt)
                images = body.get('images') or {}
                if body.get('platform') != lease['platform'] or set(images) != {item['key'] for item in lease['targets']}:
                    raise Conflict('receipt does not match this native build plan')
                for target in lease['targets']:
                    expected = target['pushRef'].rsplit(':', 1)[0] + '@'
                    image = images[target['key']]
                    if not isinstance(image, str) or not image.startswith(expected) or not DIGEST.fullmatch(image[len(expected):]):
                        raise Conflict('receipt contains an unexpected image reference')
                value = {'platform': lease['platform'], 'images': images, 'cacheRefs': [item['cacheRef'] for item in lease['targets']]}
                store.receipt(attempt['id'], value)
                return {'accepted': True}
            raise NotFound('executor endpoint not found')
        if not token or not hmac.compare_digest(token, settings.api_token):
            raise PermissionError('build platform authentication required')
        if path == '/health' and method == 'GET':
            with store.tx() as conn:
                conn.execute('SELECT 1')
            return {'status': 'ok', 'backend': 'temporal-nomad', 'maxActive': settings.max_active, 'maxPending': settings.max_pending}
        if path == '/v1/builds' and method == 'POST':
            request = body.get('request')
            if not isinstance(request, dict) or not body.get('source') or not body.get('projectKey'):
                raise Conflict('validated request, source and projectKey are required')
            for name in ('registryHost', 'pushHost'):
                if request.get(name) and request[name] != settings.registry_host:
                    raise Conflict('durable builds use the configured Harbor project; remove the registry override')
            row = store.create(request, source=body['source'], project=body['projectKey'])
            return {'queued': True, 'buildRunId': row['id'], 'run': summary(row)}
        if path == '/v1/builds' and method == 'GET':
            rows, page = store.list(query)
            return {'runs': [summary(row) for row in rows], 'page': page}
        match = re.fullmatch(r'/v1/builds/(bld2-[a-f0-9]+)(?:/(cancel|retry))?', path)
        if match:
            run_id, action = match[1], match[2]
            if method == 'GET' and not action:
                return detail(store, blobs, run_id, query)
            if method == 'POST' and action == 'cancel':
                return {'run': summary(store.cancel(run_id))}
            if method == 'POST' and action == 'retry':
                old = store.get(run_id, private=True)
                if old['status'] not in TERMINAL:
                    raise Conflict('only completed runs can be retried')
                allowed = {'envSecrets', 'workflow', 'queue', 'idempotencyKey'}
                if set(body) - allowed:
                    raise Conflict('retry may update deployment secrets/workflow only; submit a new build to change source')
                request = {**old['body'], **body}
                if not body.get('idempotencyKey'):
                    request.pop('idempotencyKey', None)
                if old['prepared'].get('sha'):
                    request['ref'] = old['prepared']['sha']
                row = store.create(request, source=old['source'], project=old['project_key'], retry_of=run_id, artifact_id=old['artifact_id'])
                if old['artifact_id']:
                    store.update(row['id'], build_status='succeeded')
                return {'queued': True, 'buildRunId': row['id'], 'run': summary(store.get(row['id']))}
        baseline = re.fullmatch(r'/v1/releases/(rel-bld2-[a-f0-9]+)/baseline', path)
        if baseline and method == 'POST':
            return {'expectedIndex': store.release_baseline(baseline[1], int(body['index']))}
        match = re.fullmatch(r'/v1/releases/(rel-bld2-[a-f0-9]+)', path)
        if match and method == 'GET':
            release = store.release(match[1])
            run = store.get(release['run_id'], private=True)
            return {'release': release, 'artifact': store.artifact(release['artifact_id']), 'request': run['body'], 'source': run['source'], 'registry': {'host': settings.registry_host, 'username': settings.registry_pull_username, 'password': settings.registry_pull_password}}
        if path == '/v1/registry/inventory' and method == 'GET':
            return harbor.inventory(page=int(query.get('page') or 1), limit=min(100, int(query.get('limit') or 50)))
        if path == '/v1/registry/gc' and method == 'POST':
            return harbor.gc(dry_run=body.get('dryRun') is not False)
        raise NotFound('build platform endpoint not found')

    async def endpoint(request: Request):
        try:
            raw = await request.body()
            if len(raw) > 1024 * 1024:
                return JSONResponse({'error': 'request too large'}, status_code=413)
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise Conflict('request body must be an object')
            auth = request.headers.get('authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else ''
            result = await run_in_threadpool(dispatch, request.method, request.url.path, token, body, dict(request.query_params))
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=401)
        except NotFound as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
        except QueueFull as exc:
            return JSONResponse({'error': str(exc)}, status_code=429, headers={'Retry-After': '10'})
        except Conflict as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
        except (LumaError, ValueError, TypeError, KeyError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    return Starlette(routes=[Route('/{path:path}', endpoint, methods=['GET', 'POST'])], lifespan=lifespan)
