/**
 * 密钥池 API 封装（Builder C）。
 *
 * 安全红线（02/03 章）：
 * - raw_key 只在 POST /api/keys 的本次请求 body 里出现一次；
 * - 永不 console、不落 localStorage、不进 pinia state；
 * - localStorage 只存后端下发的掩码字段（allowlist 白名单过滤）；
 * - account_tag 二次脱敏，仅显示首1***尾1。
 */

export const KEYS_API_BASE = 'http://localhost:8000'

export type PoolType = 'free' | 'enterprise' | 'tokenplan'
export type BackendStatus = 'TPM锁定' | 'DPAPI兜底' | '未加密'

export interface KeyUsage {
  text_n?: number
  img_n?: number
  video_s?: number
}

export interface KeyLimits {
  rpm?: number
  day_quota?: number
}

export interface KeyItem {
  id: string
  mask: string
  status: number // 2空闲 / 1使用中 / 0用光
  revoked?: boolean // C类作废
  cooling?: boolean
  cooldown_until?: string | null // A类瞬时冷却到期时间
  pool_type: string
  usage?: KeyUsage
  limits?: KeyLimits
  account_tag?: string // 后端已脱敏；前端再做一次脱敏显示
}

export interface TextModelCfg {
  provider: string
  model: string
  fallback: string
  protocol: string
  base_url: string
  temperature: number
  max_tokens: number
  timeout_s: number
}
export interface TtsCfg {
  provider: string
  voice: string
}
export interface ImageCfg {
  model: string
  size: string
  ratio: string
}
export interface VideoCfg {
  model: string
  size: string
  seconds: string
  mode: string
}
export interface ModelsConfig {
  text: TextModelCfg
  tts: TtsCfg
  image: ImageCfg
  video: VideoCfg
}

const FAKE_KEYS_LS = 'freeai.fakeKeys.v1'
const LOCAL_MODELS_LS = 'freeai.models.v1'

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

async function tryMany<T>(fns: Array<() => Promise<T>>): Promise<T> {
  let last: unknown = new Error('all endpoints failed')
  for (const fn of fns) {
    try {
      return await fn()
    } catch (e) {
      last = e
    }
  }
  throw last
}

/** GET /api/keys：兼容数组 / {keys} / {items} 三种包络；字段兼容 mask/masked、id、display。 */
export async function fetchKeysList(): Promise<{ keys: KeyItem[]; backendStatus: BackendStatus }> {
  const data = await fetchJson<unknown>(`${KEYS_API_BASE}/api/keys`)
  const d = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>
  const rawArr = Array.isArray(data) ? data : d['keys'] ?? d['items'] ?? []
  const arr = Array.isArray(rawArr) ? rawArr : []
  const keys: KeyItem[] = (arr as Array<Record<string, unknown>>).map((k, i) => {
    const mask = String(k['mask'] ?? k['masked'] ?? k['key_mask'] ?? '')
    const status = Number(k['status'] ?? 2)
    const cooling = Boolean(k['cooling'] ?? (typeof k['cooldown_left_s'] === 'number' && (k['cooldown_left_s'] as number) > 0))
    // limits 兼容两种形状：扁平 {rpm,day_quota} 与分组 {text:{rpm,day_quota},...}
    const rawLimits = (k['limits'] ?? {}) as Record<string, unknown>
    let limits: KeyLimits = {}
    if (rawLimits && typeof rawLimits === 'object' && ('text' in rawLimits || 'image' in rawLimits || 'video' in rawLimits)) {
      const pickQuota = (v: unknown): number | undefined => {
        if (v && typeof v === 'object') {
          const q = (v as Record<string, unknown>)['day_quota']
          return typeof q === 'number' ? q : undefined
        }
        return undefined
      }
      const quotas = ['text', 'image', 'video'].map((kk) => pickQuota((rawLimits as Record<string, unknown>)[kk])).filter((x): x is number => typeof x === 'number')
      const rpms = ['text', 'image', 'video'].map((kk) => {
        const v = (rawLimits as Record<string, unknown>)[kk]
        if (v && typeof v === 'object') {
          const r = (v as Record<string, unknown>)['rpm']
          return typeof r === 'number' ? r : undefined
        }
        return undefined
      }).filter((x): x is number => typeof x === 'number')
      limits = {
        ...(quotas.length ? { day_quota: Math.max(...quotas) } : {}),
        ...(rpms.length ? { rpm: Math.min(...rpms) } : {}),
      }
    } else {
      limits = (k['limits'] as KeyLimits) ?? {}
    }
    return {
      id: String(k['id'] ?? mask ?? `key-${i}`),
      mask: displayMask(mask, String(k['id'] ?? i)),
      status: Number.isFinite(status) ? status : 2,
      revoked: Boolean(k['revoked'] ?? false),
      cooling,
      cooldown_until: (k['cooldown_until'] ?? k['cooldownUntil'] ?? null) as string | null,
      pool_type: String(k['pool_type'] ?? k['poolType'] ?? 'free'),
      usage: (k['usage'] as KeyUsage) ?? {},
      limits,
      account_tag: String(k['account_tag'] ?? k['accountTag'] ?? '')
    }
  })
  const rawStatus = d['backend_status'] ?? d['backendStatus'] ?? d['security'] ?? ''
  return { keys, backendStatus: normalizeBackendStatus(rawStatus) }
}

export function normalizeBackendStatus(v: unknown): BackendStatus {
  const s = String(v ?? '')
  if (s.includes('TPM锁定') || /tpm/i.test(s)) return 'TPM锁定'
  if (s.includes('DPAPI') || /dpapi/i.test(s)) return 'DPAPI兜底'
  if (/keypool/i.test(s)) return 'DPAPI兜底'
  if (/memory-fallback/i.test(s)) return '未加密'
  return '未加密'
}

/**
 * POST /api/keys {raw_key, pool_type, account_tag}。
 * raw_key 只进本次请求 body，函数内不做任何记录、不缓存。
 */
export function addKeyRequest(payload: {
  raw_key: string
  pool_type: string
  account_tag: string
}): Promise<unknown> {
  return fetchJson<unknown>(`${KEYS_API_BASE}/api/keys`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      raw_key: payload.raw_key,
      pool_type: payload.pool_type,
      account_tag: payload.account_tag
    })
  })
}

/** 模型参数：GET /models（{local:{...}}）优先，兼容 /api/models；保存走 POST /api/models。 */
export async function fetchModelsConfig(): Promise<ModelsConfig | null> {
  try {
    const data = await tryMany<unknown>([
      () => fetchJson(`${KEYS_API_BASE}/models`),
      () => fetchJson(`${KEYS_API_BASE}/api/models`)
    ])
    // /models 回 {local:{text,...}}，取 local
    const root = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>
    const local = root['local'] && typeof root['local'] === 'object' ? root['local'] : data
    return normalizeModels(local)
  } catch {
    return null
  }
}

export async function saveModelsConfig(m: ModelsConfig): Promise<void> {
  // 后端存扁平结构；text.fallback 对象转字符串 manual
  const flat = flattenModels(m)
  const body = JSON.stringify(flat)
  const json = { 'Content-Type': 'application/json' }
  await tryMany([
    () => fetchJson(`${KEYS_API_BASE}/api/models`, { method: 'POST', headers: json, body }),
    () => fetchJson(`${KEYS_API_BASE}/models`, { method: 'POST', headers: json, body })
  ])
}

function flattenModels(m: ModelsConfig): Record<string, unknown> {
  const fb = (m.text?.fallback ?? 'manual') as unknown
  const fallback = typeof fb === 'string' ? fb : (fb && typeof fb === 'object' ? (fb as Record<string, unknown>)['mode'] ?? 'manual' : 'manual')
  return {
    text: {
      provider: m.text.provider, model: m.text.model,
      protocol: m.text.protocol || 'openai', base_url: m.text.base_url || '',
      temperature: m.text.temperature, max_tokens: m.text.max_tokens,
      timeout_s: m.text.timeout_s, fallback: { mode: String(fallback) }
    },
    tts: { provider: m.tts.provider, voice: m.tts.voice },
    image: { model: m.image.model, size: m.image.size, ratio: m.image.ratio },
    video: { model: m.video.model, size: m.video.size, seconds: m.video.seconds, mode: m.video.mode }
  }
}

/** 本地掩码：sk-前2~~~~后3。仅离线兜底/演示用；在线以后端下发掩码为准。 */
export function computeMask(raw: string): string {
  const core = String(raw || '').replace(/^sk-/, '')
  if (!core) return 'sk-~~~~'
  return `sk-${core.slice(0, 2)}~~~~${core.slice(-3)}`
}

/** 后端掩码直显（防御性截断，绝不反解、不补全）。 */
export function displayMask(mask: string, fallbackId: string): string {
  const m = String(mask || '').trim()
  if (m) return m.length > 40 ? m.slice(0, 40) : m
  return `key-${fallbackId}`
}

/** account_tag 脱敏：首1***尾1，短串只留首字。 */
export function maskAccountTag(tag: unknown): string {
  const t = String(tag ?? '').trim()
  if (!t) return '—'
  if (t.length <= 2) return `${t.charAt(0)}***`
  return `${t.charAt(0)}***${t.charAt(t.length - 1)}`
}

export function defaultModels(): ModelsConfig {
  return {
    text: { provider: '', model: '', fallback: 'manual', protocol: 'openai', base_url: '', temperature: 0.7, max_tokens: 8192, timeout_s: 120 },
    tts: { provider: 'edge-tts', voice: 'zh-CN-XiaoxiaoNeural' },
    image: { model: 'agnes-image-2.5-flash', size: '1K', ratio: '9:16' },
    video: { model: 'agnes-video-2.5-flash', size: '720P', seconds: '8', mode: 'keyframe' }
  }
}

/** 后端/本地模型配置归一化：文本允许留空（留空=拒绝任务并指引，用户显式配置前不降级）。 */
export function normalizeModels(input: unknown): ModelsConfig {
  const d = defaultModels()
  const src = (input && typeof input === 'object' ? input : {}) as Record<string, Record<string, unknown> | undefined>
  const pick = (v: unknown, fb: string): string => {
    const s = String(v ?? '').trim()
    return s || fb
  }
  const text = src['text'] ?? {}
  // fallback 兼容字符串与 {mode} 对象两种形状，绝不 "[object Object]"
  const rawFb = text['fallback'] as unknown
  const fbStr = typeof rawFb === 'string' ? rawFb : (rawFb && typeof rawFb === 'object' ? String((rawFb as Record<string, unknown>)['mode'] ?? 'manual') : 'manual')
  const num = (v: unknown, fb: number): number => {
    const n = Number(v)
    return Number.isFinite(n) ? n : fb
  }
  return {
    text: {
      provider: String(text['provider'] ?? ''),
      model: String(text['model'] ?? ''),
      fallback: fbStr || 'manual',
      protocol: String(text['protocol'] ?? '') || 'openai',
      base_url: String(text['base_url'] ?? ''),
      temperature: num(text['temperature'], 0.7),
      max_tokens: Math.max(1, Math.floor(num(text['max_tokens'], 8192))),
      timeout_s: num(text['timeout_s'], 120)
    },
    tts: {
      provider: pick(src['tts']?.['provider'], d.tts.provider),
      voice: pick(src['tts']?.['voice'], d.tts.voice)
    },
    image: {
      model: pick(src['image']?.['model'], d.image.model),
      size: pick(src['image']?.['size'], d.image.size),
      ratio: pick(src['image']?.['ratio'], d.image.ratio)
    },
    video: {
      model: pick(src['video']?.['model'], d.video.model),
      size: pick(src['video']?.['size'], d.video.size),
      seconds: pick(src['video']?.['seconds'], d.video.seconds),
      mode: pick(src['video']?.['mode'], d.video.mode)
    }
  }
}

export interface PersistStatus {
  ok: boolean
  persistent: boolean
  activated: boolean
  backend_status: string
  needs_reactivation: boolean
  db_exists: boolean
  db_path: string | null
  memory_keys: number
  already?: boolean
  migrated?: number
  archived?: string | null
  seal?: string
}

/** GET /api/keys/persist-status：激活/写透/重激活/DB/内存数。 */
export function fetchPersistStatus(): Promise<PersistStatus> {
  return fetchJson<PersistStatus>(`${KEYS_API_BASE}/api/keys/persist-status`)
}

/** POST /api/keys/activate：一键激活（force 重签，旧库解不开则归档）。 */
export function activatePool(force = false): Promise<PersistStatus> {
  return fetchJson<PersistStatus>(`${KEYS_API_BASE}/api/keys/activate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force })
  })
}

/** 无后端兜底：localStorage 假 keys（仅掩码字段）。空态返回 []，空文案由视图负责。 */
export function loadFakeKeys(): KeyItem[] {
  try {
    const raw = window.localStorage.getItem(FAKE_KEYS_LS)
    if (raw) {
      const arr = JSON.parse(raw) as unknown
      if (Array.isArray(arr)) return arr as KeyItem[]
    }
  } catch {
    /* 存储不可用则返回空 */
  }
  return []
}

/** 白名单持久化：只落掩码字段，raw_key 不可能经过本函数（调用链上游已清零）。 */
export function persistFakeKeys(keysList: KeyItem[]): void {
  try {
    const safe = keysList.map((k) => ({
      id: k.id,
      mask: k.mask,
      status: k.status,
      revoked: Boolean(k.revoked),
      cooling: Boolean(k.cooling),
      cooldown_until: k.cooldown_until ?? null,
      pool_type: k.pool_type,
      usage: k.usage ?? {},
      limits: k.limits ?? {},
      account_tag: k.account_tag ?? ''
    }))
    window.localStorage.setItem(FAKE_KEYS_LS, JSON.stringify(safe))
  } catch {
    /* 忽略持久化失败 */
  }
}

export function loadLocalModels(): ModelsConfig {
  try {
    const raw = window.localStorage.getItem(LOCAL_MODELS_LS)
    if (raw) return normalizeModels(JSON.parse(raw) as unknown)
  } catch {
    /* 解析失败则回默认 */
  }
  return defaultModels()
}

export function persistLocalModels(m: ModelsConfig): void {
  try {
    window.localStorage.setItem(LOCAL_MODELS_LS, JSON.stringify(m))
  } catch {
    /* 忽略持久化失败 */
  }
}
