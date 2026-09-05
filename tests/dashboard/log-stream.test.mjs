import assert from 'node:assert/strict';
import test from 'node:test';
import { readLogFrames, logRetryDelay, waitForLogRetry } from '../../dashboard-src/src/logStream.ts';

function chunks(bytes) {
  return new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  } });
}

test('parses byte-split UTF-8 and the final frame without a newline', async () => {
  const result = [];
  const bytes = new TextEncoder().encode('{"line":"日志 🚀","cursor":"a"}\n\n{"status":"heartbeat","cursor":"b"}');
  await readLogFrames(chunks(bytes), (frame) => result.push(frame), new AbortController().signal);
  assert.deepEqual(result, [{ line: '日志 🚀', cursor: 'a' }, { status: 'heartbeat', cursor: 'b' }]);
});

test('malformed frames fail visibly instead of silently dropping log data', async () => {
  await assert.rejects(readLogFrames(chunks(new TextEncoder().encode('{broken}\n')), () => {}, new AbortController().signal));
});

test('abort closes an idle reader without emitting stale frames', async () => {
  let cancelled = false;
  const controller = new AbortController();
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const read = readLogFrames(body, () => assert.fail('unexpected frame'), controller.signal);
  controller.abort();
  await read;
  assert.equal(cancelled, true);
});

test('retry delay backs off with a cap, and pending retries cancel immediately', async () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 12].map(logRetryDelay), [1000, 2000, 4000, 8000, 16000, 30000, 30000]);
  const controller = new AbortController();
  const waiting = waitForLogRetry(30000, controller.signal);
  controller.abort();
  await waiting;
});

test('already aborted signals neither emit frames nor wait for retries', async () => {
  const controller = new AbortController();
  controller.abort();
  await readLogFrames(chunks(new TextEncoder().encode('{"line":"stale"}\n')), () => assert.fail('unexpected frame'), controller.signal);
  await waitForLogRetry(30000, controller.signal);
});

test('joins continued Unicode fragments and displays allocation, task and stream once', async () => {
  const { appendLogFrame, formatLogLine } = await import('../../dashboard-src/src/logStream.ts');
  const source = { allocationId: 'alloc-a', task: 'web', stream: 'stdout' };
  let state = appendLogFrame([], { ...source, line: '你', partial: true }, 2000);
  state = appendLogFrame(state.lines, { ...source, line: '好', continued: true, partial: false }, 2000);
  assert.deepEqual(state.lines.map(formatLogLine), ['[alloc-a / web / stdout] 你好']);
  assert.equal(state.lines[0].partial, false);
});

test('coalesces interleaved fragments only for the exact allocation, task and stream', async () => {
  const { appendLogFrame, formatLogLine } = await import('../../dashboard-src/src/logStream.ts');
  let lines = [];
  const frames = [
    { allocationId: 'a', task: 'web', stream: 'stdout', line: 'hel', partial: true },
    { allocationId: 'b', task: 'web', stream: 'stdout', line: 'other allocation' },
    { allocationId: 'a', task: 'worker', stream: 'stdout', line: 'other task' },
    { allocationId: 'a', task: 'web', stream: 'stderr', line: 'other stream' },
    { allocationId: 'a', task: 'web', stream: 'stdout', line: 'lo', continued: true, partial: false },
  ];
  for (const frame of frames) lines = appendLogFrame(lines, frame, 2000).lines;
  assert.deepEqual(lines.map(formatLogLine), [
    '[a / web / stdout] hello', '[b / web / stdout] other allocation',
    '[a / worker / stdout] other task', '[a / web / stderr] other stream',
  ]);
});

test('a dropped beginning is marked and complete rows are never merged', async () => {
  const { appendLogFrame, formatLogLine } = await import('../../dashboard-src/src/logStream.ts');
  const source = { allocationId: 'a', task: 'web', stream: 'stdout' };
  let state = appendLogFrame([], { ...source, line: 'first', partial: true }, 1);
  state = appendLogFrame(state.lines, { ...source, stream: 'stderr', line: 'separate' }, 1);
  assert.equal(state.dropped, 1);
  state = appendLogFrame(state.lines, { ...source, line: 'remainder', continued: true }, 1);
  assert.deepEqual(state.lines.map(formatLogLine), ['[a / web / stdout] …remainder']);
  state = appendLogFrame(state.lines, { ...source, line: 'next', continued: true }, 2);
  assert.deepEqual(state.lines.map(formatLogLine), ['[a / web / stdout] …remainder', '[a / web / stdout] …next']);
});

test('identical complete messages remain separate rows', async () => {
  const { appendLogFrame, formatLogLine } = await import('../../dashboard-src/src/logStream.ts');
  const frame = { allocationId: 'a', task: 'web', stream: 'stdout', line: 'heartbeat' };
  let state = appendLogFrame([], frame, 100);
  state = appendLogFrame(state.lines, frame, 100);
  assert.equal(state.lines.length, 2);
  assert.deepEqual(state.lines.map(formatLogLine), ['[a / web / stdout] heartbeat', '[a / web / stdout] heartbeat']);
});

test('rotation cannot join a fragment from a different file of the same source', async () => {
  const { appendLogFrame, formatLogLine } = await import('../../dashboard-src/src/logStream.ts');
  const source = { allocationId: 'a', task: 'web', stream: 'stdout' };
  let state = appendLogFrame([], { ...source, file: 'stdout.0', line: 'old', partial: true }, 100);
  state = appendLogFrame(state.lines, { ...source, file: 'stdout.1', line: 'new', continued: true }, 100);
  assert.deepEqual(state.lines.map(formatLogLine), ['[a / web / stdout] old', '[a / web / stdout] …new']);
});
