import "@testing-library/jest-dom/vitest";

// Mirrors the docker-compose default so api-client tests don't warn about a
// missing NEXT_PUBLIC_API_URL; individual tests mock global.fetch anyway, so
// this value is never actually dialed.
process.env.NEXT_PUBLIC_API_URL ??= "http://localhost:8000";

// Node 22+ ships its own global `localStorage`, which on some Node versions
// shadows jsdom's window.localStorage and lacks methods like `.clear()`.
// Replace it with a small in-memory Storage implementation so
// window.localStorage behaves the same (and is reset between tests) no
// matter which Node version runs the suite.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

if (typeof window !== "undefined") {
  Object.defineProperty(window, "localStorage", {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  });
}
