import assert from 'node:assert/strict';
import test from 'node:test';
import { executions, executionPhases, mergeEvents } from '../../dashboard-src/src/components/task-observability/executionModel.ts';
import { buildTrace } from '../../dashboard-src/src/components/task-observability/traceModel.ts';
import { taskMetrics } from '../../dashboard-src/src/components/task-observability/overviewModel.ts';

test('retry recovery requires a later successful completion and preserves incomplete attempts', () => {
  const event = (trace, time, kind, extra = {}) => ({ trace_id: trace, task_id: 'task-1', timestamp: time, event: kind, ...extra });
  const rows = [event('a', 1, 'task_started'), event('a', 2, 'task_finished', { outcome: 'retrying' }), event('b', 3, 'task_started', { retry: 1 })];
  assert.equal(executions(rows)[0].outcome, 'unknown');
  assert.equal(executions([...rows, event('b', 4, 'task_finished', { outcome: 'succeeded' })])[0].outcome, 'recovered');
  assert.equal(executions([...rows, event('b', 4, 'task_finished', { outcome: 'succeeded', business_outcome: 'failed' })])[0].outcome, 'business_failed');
  assert.equal(mergeEvents(rows, rows).length, rows.length);
});

test('phase summaries retain cumulative work and an unfinished phase stays unknown', () => {
  const phases = executionPhases([
    { timestamp: 1, event: 'phase_started', stage: 'read', phase_started_at: 1 },
    { timestamp: 2, event: 'phase_finished', stage: 'read', phase_duration_ms: 120, phase_count: 2, phase_outcome: 'succeeded' },
    { timestamp: 3, event: 'phase_finished', stage: 'read', phase_duration_ms: 240, phase_count: 4, phase_outcome: 'succeeded' },
    { timestamp: 4, event: 'phase_started', stage: 'write', phase_started_at: 4 },
  ]);
  assert.equal(phases[0].durationMs, 240);
  assert.equal(phases[0].count, 4);
  assert.equal(phases[1].outcome, 'unknown');
  assert.equal(phases[1].durationMs, undefined);
});

test('waterfall subtracts overlapping child intervals once and retains orphan or cyclic spans', () => {
  const span = (id, parent, start, end) => ({ spanId: id, parentSpanId: parent, startTimeUnixNano: String(BigInt(start) * 1000000n), endTimeUnixNano: String(BigInt(end) * 1000000n) });
  const trace = buildTrace([span('root', '', 1, 101), span('a', 'root', 11, 61), span('b', 'root', 41, 81), span('orphan', 'missing', 2, 4), span('c', 'd', 2, 4), span('d', 'c', 2, 4)]);
  assert.equal(trace.rows.find(r => r.span.spanId === 'root').self, 30);
  assert.equal(trace.rows.find(r => r.span.spanId === 'orphan').orphan, true);
  assert.equal(new Set(trace.rows.map(r => r.span.spanId)).size, 6);
});

test('unavailable activity and non-finite metrics never become measured zero', () => {
  const row = (value, extra = {}) => ({ metric: { task_name: 'orders', task_framework: 'taskiq', task_queue: 'default', ...extra }, value: [100, value] });
  const tasks = taskMetrics({ registered: { state: 'ok', rows: [row('1')] }, active: { state: 'timeout', rows: [] }, duration: { state: 'ok', rows: [row('NaN')] }, executions: { state: 'ok', rows: [row('2.5', { outcome: 'failed' })] } });
  assert.equal(tasks.length, 1);
  assert.equal(tasks[0].active, undefined);
  assert.equal(tasks[0].duration, undefined);
  assert.equal(tasks[0].results.failed, 2.5);
  assert.equal(tasks[0].results.succeeded, undefined);
});
