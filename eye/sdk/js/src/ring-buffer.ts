export interface Breadcrumb {
  ts: number;
  category: string;
  level: string;
  message: string;
  data?: Record<string, unknown>;
}

/**
 * Fixed-capacity O(1) circular buffer for high-frequency breadcrumbs.
 * Keeps telemetry strictly in memory at zero network cost unless a crash/urgent flush dumps it.
 */
export class RingBuffer<T> {
  private readonly buf: (T | undefined)[];
  private readonly cap: number;
  private head = 0;
  private count = 0;

  constructor(capacity = 50) {
    this.cap = Math.max(1, capacity | 0);
    this.buf = new Array<T | undefined>(this.cap);
  }

  push(item: T): void {
    this.buf[this.head] = item;
    this.head = (this.head + 1) % this.cap;
    if (this.count < this.cap) {
      this.count++;
    }
  }

  toArray(): T[] {
    if (this.count === 0) return [];
    const out = new Array<T>(this.count);
    const start = this.count < this.cap ? 0 : this.head;
    for (let i = 0; i < this.count; i++) {
      out[i] = this.buf[(start + i) % this.cap] as T;
    }
    return out;
  }

  size(): number {
    return this.count;
  }

  clear(): void {
    this.buf.fill(undefined);
    this.head = 0;
    this.count = 0;
  }
}
