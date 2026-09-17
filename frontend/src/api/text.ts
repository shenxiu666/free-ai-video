/**
 * 文本通道连通性测试（docs/05）：POST /api/text/test。
 * 用已保存配置（可被表单值覆盖）真实 ping 一次端点；只回延迟与模型回复，不碰 Key 明文。
 */

export const TEXT_API_BASE = 'http://localhost:8000'
export const TEXT_TEST_TIMEOUT_MS = 180_000

export interface TextTestOverrides {
  provider?: string
  model?: string
  base_url?: string
  temperature?: number
  max_tokens?: number
  timeout_s?: number
}

export interface TextTestResult {
  ok: boolean
  provider: string
  model: string
  latency_ms: number
  reply: string
  generated_by: string
}

export interface TextStatus {
  ok: boolean
  provider: string
  model: string
  /** 密钥来源：密钥池（仅 Agnes Key）/ 本地 CLI（免登录）/ 自建端点专用密钥 */
  key_source: string
  custom_key: { configured: boolean; mask: string | null }
}

/** GET /api/text/status：当前文本通道 + 密钥来源 + 自建密钥是否已配。 */
export async function fetchTextStatus(): Promise<TextStatus> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), 15000)
  try {
    const resp = await fetch(`${TEXT_API_BASE}/api/text/status`, { signal: ctrl.signal })
    if (!resp.ok) throw new Error(`后端返回 ${resp.status}`)
    return (await resp.json()) as TextStatus
  } finally {
    window.clearTimeout(timer)
  }
}

/**
 * POST /api/text/custom-key：录入自建端点专用密钥（进加密池，不存配置文件）。
 * 明文只进本次请求 body，调后调用方须立即清空输入框。
 */
export async function saveCustomKey(rawKey: string): Promise<{ mask: string }> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), 15000)
  try {
    const resp = await fetch(`${TEXT_API_BASE}/api/text/custom-key`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw_key: rawKey }),
      signal: ctrl.signal
    })
    if (!resp.ok) {
      let detail = ''
      try {
        detail = String(((await resp.json()) as { detail?: unknown })?.detail ?? '')
      } catch {
        detail = ''
      }
      throw new Error(detail || `后端返回 ${resp.status}`)
    }
    return (await resp.json()) as { mask: string }
  } catch (e) {
    if (e instanceof Error) {
      if (e.name === 'AbortError') throw new Error('请求超时：请确认后端已在 http://localhost:8000 启动')
      if (e.message.includes('Failed to fetch')) throw new Error('无法连接后端：请确认后端已在 http://localhost:8000 启动')
    }
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

export async function testTextConnectivity(overrides: TextTestOverrides = {}): Promise<TextTestResult> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), TEXT_TEST_TIMEOUT_MS)
  try {
    const resp = await fetch(`${TEXT_API_BASE}/api/text/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(overrides),
      signal: ctrl.signal
    })
    if (!resp.ok) {
      let detail = ''
      try {
        const data = (await resp.json()) as { detail?: unknown }
        detail = String(data?.detail ?? '').slice(0, 300)
      } catch {
        try {
          detail = (await resp.text()).slice(0, 300)
        } catch {
          detail = ''
        }
      }
      throw new Error(detail || `后端返回 ${resp.status}，请检查后端日志`)
    }
    return (await resp.json()) as TextTestResult
  } catch (e) {
    if (e instanceof Error) {
      if (e.name === 'AbortError') throw new Error('连通性测试超时（180s）：端点无响应或网络不通')
      if (e.message.includes('Failed to fetch') || e.message.includes('NetworkError') || e.message.includes('Load failed'))
        throw new Error('无法连接后端：请确认后端已在 http://localhost:8000 启动')
    }
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}
