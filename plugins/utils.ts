/**
 * Minimal stubs of the Portkey gateway plugin utilities used by the Straiker
 * handler. When the plugin is dropped into a Portkey checkout, Portkey's
 * own `plugins/utils.ts` is the canonical source. These stubs only exist so
 * this repo compiles and the unit tests run offline.
 */

export class HttpError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
  }
}

export class TimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TimeoutError';
  }
}

interface PostOptions {
  headers?: Record<string, string>;
}

export async function post<T>(
  url: string,
  body: unknown,
  options: PostOptions = {},
  timeoutMs = 5000
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new HttpError(`POST ${url} failed: ${r.status} ${text}`, r.status);
    }
    return (await r.json()) as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new TimeoutError(`POST ${url} timed out after ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
