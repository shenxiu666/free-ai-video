/**
 * Builder B · drama REST 封装
 *
 * 后端基址 http://localhost:8000，单次请求超时 15s，失败一律抛中文错误。
 *
 * 约定路由（POST /api/drama/new 与 GET /api/drama/{name}/state 来自任务书；
 * 保存走 POST /api/drama/{name}/script，待与后端联调确认，不对就改这一处）：
 *   POST /api/drama/new            新建剧
 *   GET  /api/drama/{name}/state   读取 state.json（含 script）
 *   POST /api/drama/{name}/script  保存 script.json
 *   GET  /models                   模型配置（含 text.provider/model 是否留空）
 */

export const API_BASE = 'http://localhost:8000';
export const REQUEST_TIMEOUT_MS = 15_000;

export interface NewDramaPayload {
  title: string;
  total_seconds: number;
  aspect: '9:16' | '16:9';
  /** 传字符串，与 script.json 契约一致（"4"-"12"） */
  clip_seconds: string;
  style: string;
  brief: string;
}

export interface DramaStateResponse {
  script?: unknown;
  state?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface TextConfig {
  provider: string;
  model: string;
}

function toChineseError(e: unknown, path: string): Error {
  if (e instanceof Error) {
    if (e.name === 'AbortError') {
      return new Error(`请求超时：${path}，AI 分镜可能需 1-2 分钟，可重试或改短原文；请确认后端已在 http://localhost:8000 启动`);
    }
    const msg = e.message || '';
    if (
      msg.includes('Failed to fetch') ||
      msg.includes('NetworkError') ||
      msg.includes('Load failed') ||
      msg.includes('Network request failed')
    ) {
      return new Error(`无法连接后端（${path}）：请确认后端已在 http://localhost:8000 启动`);
    }
    return e;
  }
  return new Error(`请求失败（${path}）：未知错误`);
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json', ...(init.headers || {}) },
    });
    if (!res.ok) {
      let detail = '';
      try {
        detail = (await res.text()).slice(0, 300);
      } catch {
        detail = '';
      }
      throw new Error(`后端返回 ${res.status}（${path}）：${detail || res.statusText || '无详情'}`);
    }
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Error(`后端返回的不是合法 JSON（${path}），请检查后端日志`);
    }
  } catch (e) {
    throw toChineseError(e, path);
  } finally {
    window.clearTimeout(timer);
  }
}

/** POST /api/drama/new 新建剧 */
export function createDrama(payload: NewDramaPayload): Promise<unknown> {
  return request<unknown>('/api/drama/new', { method: 'POST', body: JSON.stringify(payload) });
}

/** GET /api/drama/{name}/state 读取剧状态 */
export function fetchDramaState(name: string): Promise<DramaStateResponse> {
  return request<DramaStateResponse>(`/api/drama/${encodeURIComponent(name)}/state`);
}

/** POST /api/drama/{name}/script 保存 script.json */
export function saveDramaScript(name: string, script: unknown): Promise<unknown> {
  return request<unknown>(`/api/drama/${encodeURIComponent(name)}/script`, {
    method: 'POST',
    body: JSON.stringify(script),
  });
}

/** GET /models 模型配置（用于判断 text.provider/model 是否留空） */
export function fetchModels(): Promise<unknown> {
  return request<unknown>('/models');
}

export interface BreakdownPayload {
  name: string;
  total_seconds: number;
  aspect: '9:16' | '16:9';
  clip_seconds: string;
  style: string;
  source_text: string;
}

export interface BreakdownResponse {
  ok: boolean;
  script: unknown;
  generated_by?: string;
  latency_ms?: number;
  source_chars?: number;
  truncated?: boolean;
}

/**
 * POST /api/drama/breakdown AI 分镜：原文 → script.json 初稿（不落盘）。
 * 模型输出长 JSON，超时放宽到 180s。
 */
export function breakdownDrama(payload: BreakdownPayload): Promise<BreakdownResponse> {
  return request<BreakdownResponse>(
    '/api/drama/breakdown',
    { method: 'POST', body: JSON.stringify(payload) },
    180_000,
  );
}

function pickTextNode(root: Record<string, unknown>): Record<string, unknown> | null {
  // 后端 GET /models 回 {local:{text:{...}}, opencode:{...}}（main.py），优先读 local.text
  const local = root['local'];
  if (local && typeof local === 'object' && !Array.isArray(local)) {
    const t = (local as Record<string, unknown>)['text'];
    if (t && typeof t === 'object' && !Array.isArray(t)) return t as Record<string, unknown>;
  }
  const direct = root['text'];
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    return direct as Record<string, unknown>;
  }
  const config = root['config'];
  if (config && typeof config === 'object' && !Array.isArray(config)) {
    const nested = (config as Record<string, unknown>)['text'];
    if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
      return nested as Record<string, unknown>;
    }
  }
  // 兼容列表形：[{ kind/scope: 'text', provider, model }]
  for (const value of Object.values(root)) {
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item && typeof item === 'object') {
          const rec = item as Record<string, unknown>;
          if (rec['kind'] === 'text' || rec['scope'] === 'text') return rec;
        }
      }
    }
  }
  return null;
}

/**
 * 从 /models 响应提取 text.provider/model。
 * 返回 null 表示“结构不可识别”（视为未知，走警告不断行）；
 * 返回空字符串表示“明确留空”（调用方必须内联拒绝并指引去 /keys）。
 */
export function pickTextConfig(models: unknown): TextConfig | null {
  if (!models || typeof models !== 'object' || Array.isArray(models)) return null;
  const node = pickTextNode(models as Record<string, unknown>);
  if (!node) return null;
  const provider = typeof node['provider'] === 'string' ? (node['provider'] as string).trim() : '';
  const model = typeof node['model'] === 'string' ? (node['model'] as string).trim() : '';
  return { provider, model };
}
