"""Python 3.9 compatible gateway. Execution belongs to the build service."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ..errors import LumaError


def enabled() -> bool:
    return configured() and os.environ.get('LUMA_BUILD_PLATFORM_ENABLED', '1').lower() not in {'0', 'false', 'no'}


def configured() -> bool:
    return bool(os.environ.get('LUMA_BUILD_PLATFORM_URL', '').strip())


def request(method, path, body=None, query=None):
    base = os.environ.get('LUMA_BUILD_PLATFORM_URL', '').rstrip('/')
    token = os.environ.get('LUMA_BUILD_API_TOKEN', '')
    if not base or not token:
        raise LumaError('durable build platform is not configured')
    url = base + path + ('?' + urllib.parse.urlencode(query) if query else '')
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get('error', '')
        except (ValueError, TypeError):
            message = ''
        raise LumaError(message or f'build platform returned HTTP {exc.code}') from exc
    except (OSError, ValueError) as exc:
        raise LumaError('build platform unavailable; this request was not sent to the legacy builder') from exc


def project(run_id):
    from . import server
    result = request('GET', '/v1/builds/' + run_id, query={'limit': 1})
    run = dict(result['run'])
    run.pop('events', None)
    # SQLite is only a read projection for the combined legacy/new history.
    # Never hydrate or copy raw logs into Control.
    def mutate(state):
        runs = server._build_runs(state)
        previous = runs.get(run_id)
        if previous != run:
            runs[run_id] = run
    server._mutate_control_state(mutate)
    return result


def prepare(token, body):
    from . import server as s
    state = s.load_state()
    s.require_token(state, token, token_type='deploy')
    s._reject_control_maintenance(state)
    s._apply_state_secrets(state)
    provider_id = str(body.get('providerId') or '')
    provider = s._require_git_provider(state, provider_id) if provider_id else {}
    repo_url = s.normalize_import_repo_url(str(body.get('repoUrl') or ''))
    if not repo_url:
        if not provider or not body.get('repository'):
            raise LumaError('repoUrl or providerId + repository is required')
        repo_url = s._git_provider_clone_url(provider, body['repository'])
    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise LumaError('repository must be an HTTPS URL without inline credentials')
    if body.get('composeSidecar'):
        s.normalize_repo_relative_path(body['composeSidecar'], label='composeSidecar')
        if body.get('manifest'):
            raise LumaError('composeSidecar cannot be combined with manifest')
    if provider and urllib.parse.urlsplit(s._git_provider_clone_base(provider)).netloc.lower() != parsed.netloc.lower():
        raise LumaError('repository host does not match the selected Git credential')
    mode = str(body.get('proxyMode') or 'auto')
    if mode not in {'auto', 'direct'}:
        raise LumaError('proxyMode must be auto or direct')
    from .build_pool import target_inventory
    inventory = target_inventory(state)
    build_node = str(body.get('buildNode') or '')
    if build_node and build_node != 'auto':
        from .build_pool import declared_names
        if build_node not in declared_names(state):
            raise LumaError('build node is not in the configured build-node list: ' + build_node)
    s._request_env_secrets(body)
    # The pool is independently placed. Its egress endpoint is configured on
    # this gateway rather than inferred from the retired singleton builder.
    proxy = '' if mode == 'direct' else str(body.get('proxy', os.environ.get('LUMA_BUILD_EGRESS_PROXY', '')))
    return {'repoUrl': repo_url, 'projectKey': parsed.netloc.lower() + '/' + s._image_repo_from_repo_url(repo_url),
        'gitToken': str(provider.get('token') or (((state.get('secrets') or {}).get('GITHUB_TOKEN') or os.environ.get('GITHUB_TOKEN')) if parsed.hostname == 'github.com' else '') or ''), 'gitUsername': str(provider.get('username') or ''),
        'proxy': proxy, 'targetPlatformsByNode': inventory['byNode'], 'targetPlatformsByRegion': inventory['byRegion']}


def submit(token, body):
    context = prepare(token, body)
    value = request('POST', '/v1/builds', {'request': {**body, 'repoUrl': context['repoUrl']}, 'source': context['repoUrl'], 'projectKey': context['projectKey']})
    # Admission is committed even if this projection temporarily fails. The
    # durable worker retries projection before completing the workflow.
    try:
        project(value['buildRunId'])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Build admitted; history projection will be retried")
    return value


def publish(token, body):
    import yaml
    from . import server as s
    from ..release import release_context
    s.require_token(s.load_state(), token, token_type='deploy')
    release_id = str(body.get('releaseId') or '')
    if not release_id.startswith('rel-bld2-'):
        raise LumaError('invalid build release ID')
    # Include preparation and all external publication effects in the existing
    # Control deployment lock. Build work and its capacity remain independent.
    with s._DEPLOY_LOCK:
        value = request('GET', '/v1/releases/' + release_id)
        release, artifact = value['release'], value['artifact']['result']
        if release['generation'] < release['desired_generation']:
            return {'superseded': True, 'releaseId': release_id}
        if release['status'] == 'succeeded':
            return release['result']
        manifest = yaml.safe_load(artifact['manifest'])
        if s.slugify(str(manifest.get('name') or '')) != release['target_key']:
            raise LumaError('artifact target does not match its release')
        original = value['request']
        for key in ('region',) if artifact['kind'] == 'compose' else ('region', 'exposure', 'domain', 'port'):
            if original.get(key):
                manifest[key] = original[key]
        deploy_body = {**original, 'manifest': yaml.safe_dump(manifest, sort_keys=False), 'sourceName': value['source']}
        deploy_body.pop('repoUrl', None)
        deploy_body['gitSource'] = s._git_source_from_build_body(original, repo_url=value['source'], build_node='auto', build_run_id=release['run_id'])
        context = {**release, 'registry': value['registry']}
        marker = release_context.set(context)
        try:
            if artifact['kind'] == 'compose':
                deploy_body['composeContent'] = artifact['composeContent']
                result = s.handle_compose_deployment(token, deploy_body)
            else:
                result = s.handle_deployment(token, deploy_body)
            workflow = original.get('workflow')
            if isinstance(workflow, dict):
                try:
                    name = result.get('service') or result.get('deployment')
                    s.handle_workflow_record(token, {**workflow, 'name': name, 'source': 'cli-success',
                        'evidence': {'image': artifact.get('image'), 'buildId': release['run_id'], 'revision': result.get('revision')}})
                    result['workflow'] = {'saved': True, 'name': name}
                except Exception as exc:
                    result['workflow'] = {'saved': False, 'warning': str(exc)}
            return {**result, 'buildRunId': release['run_id'], 'releaseId': release_id,
                'image': artifact.get('image', ''), 'images': artifact.get('images', {})}
        finally:
            release_context.reset(marker)


def dispatch(token, path, body):
    from . import server as s
    s.require_token(s.load_state(), token, token_type='deploy')
    if not configured():
        raise LumaError('durable build platform is not enabled')
    if path == 'pool':
        from .build_pool import inventory
        return {'nodes': inventory(s.load_state())}
    if path == 'prepare':
        return prepare(token, body)
    if path == 'publish':
        return publish(token, body)
    if path == 'project':
        return project(str(body.get('buildRunId') or ''))
    raise LumaError('unknown build platform operation')


def retryable_publication_error(error):
    """Preserve uncertainty across the internal HTTP boundary.

    The ordinary Control API represents domain errors as HTTP 400. A lost
    Nomad/registry reply during durable publication must instead be retryable.
    """
    visited = set()
    while error is not None and id(error) not in visited:
        visited.add(id(error))
        if isinstance(error, urllib.error.HTTPError):
            if error.code in {408, 409, 429} or error.code >= 500:
                return True
        elif isinstance(error, (urllib.error.URLError, ConnectionError, TimeoutError)):
            return True
        error = error.__cause__ or error.__context__
    return False
