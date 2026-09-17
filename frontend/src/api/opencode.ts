/**
 * opencode 二进制选择 API（docs/05 §5.8）。
 * opencode 仅为开发期 Agent，运行时不强依赖；路径非秘密值，可存 localStorage。
 */

export const OPENCODE_API_BASE = 'http://localhost:8000'

export type OpencodeMode = 'builtin' | 'system' | 'custom' | 'auto'

export interface OpencodeCandidate {
  source: string
  path: string
  exists: boolean
  version?: string | null
  error?: string | null
}

export interface OpencodeEffective {
  path: string | null
  source: string | null
  reason: string
  exists: boolean
  version: string | null
  pinned: string
  pinned_match: boolean | null
}

export interface OpencodeStatus {
  selection: { mode: string; bin_path: string }
  effective: OpencodeEffective
  pinned_version: string
  candidates: OpencodeCandidate[]
}

export interface OpencodeTestResult {
  ok: boolean
  path: string
  version: string | null
  latency_ms: number
  error: string | null
}

const LOCAL_STATUS_LS = 'freeai.opencode.status.v1'

async function fetchJson<T>(url: string, init?: RequestInit, timeoutMs = 15000): Promise<T> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const resp = await fetch(url, { ...init, signal: ctrl.signal })
    if (!resp.ok) {
      let detail = ''
      try {
        detail = (await resp.text()).slice(0, 200)
      } catch {
        detail = ''
      }
      throw new Error(`后端返回 ${resp.status}（${url}）：${detail || resp.statusText || '无详情'}`)
    }
    return (await resp.json()) as T
  } catch (e) {
    if (e instanceof Error) {
      if (e.name === 'AbortError') throw new Error(`请求超时（15s）：${url}，请确认本地后端已在 http://localhost:8000 启动`)
      if (e.message.includes('Failed to fetch') || e.message.includes('NetworkError') || e.message.includes('Load failed'))
        throw new Error(`无法连接后端（${url}）：请确认后端已在 http://localhost:8000 启动`)
    }
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

/** GET /api/opencode/status：选择+生效项+锁定版本+候选列表（快，不逐项跑版本）。 */
export function fetchOpencodeStatus(): Promise<OpencodeStatus> {
  return fetchJson<{ ok: boolean } & OpencodeStatus>(`${OPENCODE_API_BASE}/api/opencode/status`)
}

/** POST /api/opencode/detect：全量版本探测（设置页“检测”按钮，每项限时）。 */
export function detectOpencode(timeoutMs = 60000): Promise<{ candidates: OpencodeCandidate[] }> {
  return fetchJson<{ ok: boolean; candidates: OpencodeCandidate[] }>(
    `${OPENCODE_API_BASE}/api/opencode/detect`, { method: 'POST' }, timeoutMs
  )
}

/** POST /api/opencode/select：保存 mode/bin_path 到 config/opencode.yaml。 */
export function selectOpencode(mode: OpencodeMode, bin_path = ''): Promise<OpencodeStatus> {
  return fetchJson<{ ok: boolean } & OpencodeStatus>(`${OPENCODE_API_BASE}/api/opencode/select`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, bin_path })
  })
}

/** POST /api/opencode/test：跑 `<bin> --version` 验证（缺省测当前生效项）。 */
export function testOpencode(bin_path = ''): Promise<OpencodeTestResult> {
  return fetchJson<{ ok: boolean } & OpencodeTestResult>(`${OPENCODE_API_BASE}/api/opencode/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bin_path: bin_path || null })
  })
}

export interface FreeModel {
  provider: string
  model: string
  ref: string
  name: string
  free: boolean | null
  url: string
}

export interface FreeModelsResponse {
  ok: boolean
  source: 'live' | 'curated'
  bin: string | null
  models: FreeModel[]
  note: string
}

/**
 * GET /api/opencode/models：拉取可用模型（默认 opencode 免费通道）。
 * live 失败后端自动回内置候选；refresh 从 models.dev 刷新（较慢）。
 */
export function fetchFreeModels(provider = 'opencode', refresh = false): Promise<FreeModelsResponse> {
  const q = `provider=${encodeURIComponent(provider)}${refresh ? '&refresh=true' : ''}`
  return fetchJson<FreeModelsResponse>(
    `${OPENCODE_API_BASE}/api/opencode/models?${q}`, {}, refresh ? 90000 : 45000
  )
}

export function loadLocalOpencodeStatus(): OpencodeStatus | null {
  try {
    const raw = window.localStorage.getItem(LOCAL_STATUS_LS)
    if (raw) return JSON.parse(raw) as OpencodeStatus
  } catch {
    /* 解析失败则回 null */
  }
  return null
}

export function persistLocalOpencodeStatus(s: OpencodeStatus): void {
  try {
    window.localStorage.setItem(LOCAL_STATUS_LS, JSON.stringify(s))
  } catch {
    /* 忽略持久化失败 */
  }
}

export const OPENCODE_MODE_LABELS: Record<OpencodeMode, string> = {
  builtin: '内置（默认）',
  system: '系统 PATH',
  custom: '自定义路径',
  auto: '自动（内置→PATH）'
}
