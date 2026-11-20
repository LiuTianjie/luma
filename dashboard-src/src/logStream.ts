/** NDJSON may split UTF-8 characters and frames across arbitrary network chunks. */
export async function readLogFrames(
  body: ReadableStream<Uint8Array>,
  onFrame: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (flush = false) => {
    const parts = buffer.split("\n");
    buffer = flush ? "" : parts.pop() || "";
    for (const part of parts) {
      if (!part.trim() || signal.aborted) continue;
      const event: unknown = JSON.parse(part);
      if (!event || typeof event !== "object" || Array.isArray(event)) throw new Error("Invalid log stream frame");
      onFrame(event as Record<string, unknown>);
    }
  };
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (signal.aborted) return;
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      consume(done);
      if (done) return;
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function logRetryDelay(attempt: number) {
  return Math.min(30000, 1000 * 2 ** Math.min(Math.max(attempt, 0), 5));
}

export function waitForLogRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, delay);
    signal.addEventListener("abort", finish, { once: true });
  });
}

export type DisplayLogLine = {
  text: string;
  sourceKey: string;
  file: string;
  label: string;
  partial: boolean;
};

/** Preserve source identity while joining a logical line split across polls.
 * Search only the latest row of this source: a later complete row or rotation
 * must never cause a continuation to attach to an older abandoned fragment.
 */
export function appendLogFrames(
  previous: DisplayLogLine[],
  frames: Record<string, unknown>[],
  maxLines: number,
): { lines: DisplayLogLine[]; dropped: number } {
  if (!frames.length) return { lines: previous, dropped: 0 };
  const limit = Math.max(1, maxLines);
  const lines = [...previous];
  let head = 0;
  let dropped = 0;
  for (const frame of frames) {
    if (typeof frame.line !== "string") continue;
    const source = [frame.allocationId, frame.task, frame.stream].map((value) => typeof value === "string" ? value : "");
    const sourceKey = JSON.stringify(source);
    const label = source.filter(Boolean).join(" / ");
    const file = typeof frame.file === "string" ? frame.file : "";
    let merged = false;
    if (frame.continued === true && label) {
      for (let index = lines.length - 1; index >= head; index -= 1) {
        const row = lines[index];
        if (row.sourceKey !== sourceKey) continue;
        if (row.partial && row.file === file) {
          lines[index] = { ...row, text: row.text + frame.line, partial: frame.partial === true };
          merged = true;
        }
        break;
      }
    }
    if (!merged) {
      lines.push({
        sourceKey, file, label, partial: frame.partial === true,
        text: (frame.continued === true ? "…" : "") + frame.line,
      });
      const liveLen = lines.length - head;
      if (liveLen > limit) {
        const excess = liveLen - limit;
        head += excess;
        dropped += excess;
      }
    }
  }
  return { lines: head > 0 ? lines.slice(head) : lines, dropped };
}

export function appendLogFrame(
  previous: DisplayLogLine[],
  frame: Record<string, unknown>,
  maxLines: number,
): { lines: DisplayLogLine[]; dropped: number } {
  if (typeof frame.line !== "string") return { lines: previous, dropped: 0 };
  return appendLogFrames(previous, [frame], maxLines);
}

export function formatLogLine(line: DisplayLogLine): string {
  return line.label ? `[${line.label}] ${line.text}` : line.text;
}
