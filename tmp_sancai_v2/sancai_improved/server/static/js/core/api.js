// ══════════════════════════════════════════════════════════════
// API client — thin wrapper over fetch with timeout + error typing
// ══════════════════════════════════════════════════════════════

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

const DEFAULT_TIMEOUT = 15000;

async function request(path, { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT, signal } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // allow external cancellation (e.g. task cancel buttons) to compose with timeout
  if (signal) signal.addEventListener('abort', () => controller.abort());

  try {
    const resp = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const isJson = resp.headers.get('content-type')?.includes('application/json');
    const payload = isJson ? await resp.json().catch(() => null) : await resp.text();
    if (!resp.ok) {
      let msg = payload?.detail || payload?.message || `请求失败 (${resp.status})`;
      // detail 可能是对象（如策略校验返回 {errors:[...], warnings:[...]}），转成可读文本
      if (msg && typeof msg === 'object') {
        if (Array.isArray(msg.errors) && msg.errors.length) msg = msg.errors.join('；');
        else if (Array.isArray(msg)) msg = msg.map((x) => (typeof x === 'object' ? (x.msg || JSON.stringify(x)) : x)).join('；');
        else msg = msg.msg || JSON.stringify(msg);
      }
      throw new ApiError(msg, resp.status, payload);
    }
    return payload;
  } catch (e) {
    if (e.name === 'AbortError') throw new ApiError('请求超时', 0, null);
    if (e instanceof ApiError) throw e;
    throw new ApiError(e.message || '网络错误', 0, null);
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body, timeoutMs: opts?.timeoutMs ?? 30000 }),
  del: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
  put: (path, body, opts) => request(path, { ...opts, method: 'PUT', body }),
};

/** Poll a status endpoint until predicate(result) is true or timeout/cancel. */
export function poll(fn, { intervalMs = 1500, maxMs = 300000, until, onTick } = {}) {
  let cancelled = false;
  let timerId = null;
  const start = Date.now();

  const promise = new Promise((resolve, reject) => {
    async function tick() {
      if (cancelled) return reject(new ApiError('已取消', 0, null));
      try {
        const result = await fn();
        if (cancelled) return reject(new ApiError('已取消', 0, null));
        onTick?.(result);
        if (until(result)) return resolve(result);
        if (Date.now() - start > maxMs) return reject(new ApiError('轮询超时', 0, null));
        timerId = setTimeout(tick, intervalMs);
      } catch (e) {
        if (timerId) { clearTimeout(timerId); timerId = null; }
        reject(e);
      }
    }
    tick();
  });

  return {
    promise,
    cancel: () => {
      cancelled = true;
      if (timerId) { clearTimeout(timerId); timerId = null; }
    },
  };
}
