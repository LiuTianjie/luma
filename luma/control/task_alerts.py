"""Fixed SDK metric queries for application alerts; no user-supplied PromQL."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

PRESETS = [
    dict(metric='app.task_heartbeat_age', name='任务服务心跳中断', description='指定 OTel 服务中最新进程心跳距今；缺失也可能是采集链路不可用', threshold=120, forSeconds=180, unit='seconds'),
    dict(metric='app.task_queue_wait_p95', name='任务排队等待 P95', description='近 15 分钟已消费任务排队 P95；每个服务/队列至少 20 次样本，取最大值', threshold=300, forSeconds=300, unit='seconds'),
    dict(metric='app.task_telemetry_dropped', name='任务 SDK 遥测丢弃', description='近 15 分钟 SDK 生命周期日志丢弃及埋点异常增量；不含全部 OTLP/Collector 丢弃', threshold=10, forSeconds=0, unit='count'),
    dict(metric='app.task_tracking_overflow', name='活跃任务跟踪溢出', description='有新鲜心跳的进程中，当前未被 SDK 跟踪的活跃任务数', threshold=0, forSeconds=300, unit='count'),
    dict(metric='app.task_completion_missing', name='周期任务完成观测中断', description='预期按固定周期执行的任务在统计窗口内没有完成记录；需结合心跳排查调度、执行和采集链路', threshold=0, forSeconds=180, unit='count'),
]
METRICS = {p['metric'] for p in PRESETS}
MAX_RULES = 20


def normalize(metric, target, config):
    if target == '*':
        raise ValueError('Task metric rules require one application ID')
    if not isinstance(config, dict):
        raise ValueError('taskQuery must be an object')
    service = config.get('service', '')
    if not isinstance(service, str) or len(service) > 160 or any(ord(c) < 32 for c in service):
        raise ValueError('Invalid taskQuery.service')
    service = service.strip()
    if metric in ('app.task_heartbeat_age', 'app.task_completion_missing') and not service:
        raise ValueError('Heartbeat rules require the expected OTel service name')
    if metric == 'app.task_completion_missing':
        name = config.get('taskName', '')
        framework = config.get('framework', '')
        window = config.get('windowSeconds', 300)
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
            raise ValueError('Completion rules require an expected periodic task name')
        if framework not in ('taskiq', 'celery'):
            raise ValueError('taskQuery.framework must be taskiq or celery')
        if isinstance(window, bool) or not isinstance(window, int) or not 60 <= window <= 3600:
            raise ValueError('taskQuery.windowSeconds must be an integer from 60 to 3600')
        return {'service': service, 'taskName': name.strip(), 'framework': framework, 'windowSeconds': window}
    return {'service': service}


def expression(metric, target, config):
    config = normalize(metric, target, config)
    scope = 'luma_stack=' + json.dumps(target)
    if config['service']:
        scope += ',service_name=' + json.dumps(config['service'])
    if metric == 'app.task_completion_missing':
        scope += ',task_name=' + json.dumps(config['taskName']) + ',task_framework=' + json.dumps(config['framework'])
        counter = 'task_executions_total' if config['framework'] == 'taskiq' else 'task_span_calls_total'
        if config['framework'] == 'celery':
            scope += ',luma_observe_span_metrics="service"'
        # Sum per-series increases so process replacement and counter resets are
        # handled independently. Missing series stay missing for noData policy.
        return f'(sum by(luma_stack)(increase({counter}{{{scope}}}[{config["windowSeconds"]}s])) == bool 0)'
    heartbeat = f'worker_heartbeat_seconds{{{scope}}}'
    live = f'max by(luma_stack,service_name)({heartbeat} > time()-120)'
    if metric == 'app.task_heartbeat_age':
        # An explicitly configured service remains expected when all its series
        # disappear. The rule's noData policy decides how to handle that state.
        return f'clamp_min(time()-max by(luma_stack)({heartbeat}),0)'
    if metric == 'app.task_queue_wait_p95':
        group = 'luma_stack,service_name,task_queue'
        count = f'sum by({group})(increase(task_queue_wait_seconds_count{{{scope},task_framework="taskiq"}}[15m]))'
        buckets = f'sum by({group},le)(rate(task_queue_wait_seconds_bucket{{{scope},task_framework="taskiq"}}[15m]))'
        # Observed low traffic clears a latency alert; missing telemetry does
        # not. Do not mix unrelated queues into one latency distribution.
        qualified = f'(histogram_quantile(0.95,{buckets}) and ({count} >= 20)) or (0 * {count})'
        return f'max by(luma_stack)(({qualified}) and on(luma_stack,service_name) {live})'
    if metric == 'app.task_telemetry_dropped':
        values = f'sum by(luma_stack,service_name)(increase(observe_sdk_dropped_total{{{scope}}}[15m]))'
        return f'sum by(luma_stack)({values} and on(luma_stack,service_name) {live})'
    if metric == 'app.task_tracking_overflow':
        return (f'sum by(luma_stack)(task_active_tracking_overflow{{{scope}}} '
                f'and on(luma_stack,service_name,service_instance_id) ({heartbeat} > time()-120))')
    raise ValueError('Unsupported task alert metric')


def hydrate(state, rules):
    """Bound queries outside SQLite and reuse identical queries across rules."""
    from .observe import instant_values
    expressions = {r['id']: expression(r['metric'], r['target'], r['taskQuery']) for r in rules}
    unique = list(dict.fromkeys(expressions.values()))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='task-alert') as pool:
        values = dict(zip(unique, pool.map(lambda expr: instant_values(state, expr, 'luma_stack'), unique)))
    return {r['id']: values[expressions[r['id']]].get(r['target']) for r in rules}
