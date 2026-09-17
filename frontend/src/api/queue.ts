/**
 * 渲染队列 API 封装（Builder C）。
 * base 固定 http://localhost:8000；仅做 fetch / EventSource 封装。
 * 重连退避、轮询兜底、假进度由 stores/queue.ts 调度。
 */

export const QUEUE_API_BASE = 'http://localhost:8000'

export type StageName = 'image' | 'video' | 'tts' | 'mux'
export type StageStatus = 'pending' | 'doing' | 'done' | 'failed'

export interface ClipStateDTO {
  id: string
  image: StageStatus
  video: StageStatus
  tts: StageStatus
  mux: StageStatus
  progress?: number
  message?: string
}

export interface FinalEntryDTO {
  file: string
  created_at?: string
}

export interface MuxDetailDTO {
  missing_dubs: string[]
  missing_srts: string[]
  dub_ok: number
  dub_total: number
  duration_error?: number | null
  verified?: boolean
}

export interface DramaStateDTO {
  name: string
  total_seconds?: number
  clips: ClipStateDTO[]
  updated_at?: string
  finals?: FinalEntryDTO[]
  final_exists?: boolean
  mux_detail?: MuxDetailDTO
}

export interface QueueEventDTO {
  type: string
  clip_id?: string
  clipId?: string
  stage?: StageName
  status?: StageStatus
  level?: string
  message?: string
  msg?: string
  time?: string
  state?: DramaStateDTO
  clips?: ClipStateDTO[]
}

export class QueueHttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'QueueHttpError'
    this.status = status
  }
}

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
      throw new QueueHttpError(resp.status, `后端返回 ${resp.status}（${url}）：${detail || resp.statusText || '无详情'}`)
    }
    return (await resp.json()) as T
  } catch (e) {
    if (e instanceof QueueHttpError) throw e
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

/** 是否连不上后端（相对 404 剧不存在而言的真·不可达）。 */
export function isOfflineError(e: unknown): boolean {
  if (e instanceof QueueHttpError) return false
  const m = e instanceof Error ? e.message : String(e)
  return m.includes('无法连接后端') || m.includes('请求超时') || m.includes('Failed to fetch')
}

/** GET /api/drama/{name}/state：兼容 {ok,state} 包络与裸 DTO（含 orchestrator dict 形 clips）。 */
export function fetchDramaState(name: string): Promise<DramaStateDTO> {
  return fetchJson<{ ok?: boolean; state?: unknown; name?: string; clips?: unknown } | DramaStateDTO>(
    `${QUEUE_API_BASE}/api/drama/${encodeURIComponent(name)}/state`
  ).then((raw) => normalizeState(raw, name))
}

function asStage(s: unknown): StageStatus {
  return s === 'doing' || s === 'done' || s === 'failed' ? s : 'pending'
}

function asFinals(v: unknown): FinalEntryDTO[] | undefined {
  if (!Array.isArray(v)) return undefined
  const out: FinalEntryDTO[] = []
  for (const e of v as Array<Record<string, unknown>>) {
    if (!e || typeof e !== 'object') continue
    const file = e['file']
    if (typeof file !== 'string' || !file) continue
    const created = e['created_at']
    out.push(typeof created === 'string' ? { file, created_at: created } : { file })
  }
  return out
}

function asMuxDetail(v: unknown): MuxDetailDTO | undefined {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined
  const m = v as Record<string, unknown>
  const strArr = (x: unknown): string[] =>
    Array.isArray(x) ? (x as unknown[]).filter((i): i is string => typeof i === 'string') : []
  const num = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
  const detail: MuxDetailDTO = {
    missing_dubs: strArr(m['missing_dubs']),
    missing_srts: strArr(m['missing_srts']),
    dub_ok: num(m['dub_ok']),
    dub_total: num(m['dub_total'])
  }
  const de = m['duration_error']
  if (typeof de === 'number' && Number.isFinite(de)) detail.duration_error = de
  else if (de === null) detail.duration_error = null
  if (typeof m['verified'] === 'boolean') detail.verified = m['verified'] as boolean
  return detail
}

function normalizeState(raw: unknown, fallbackName: string): DramaStateDTO {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const st = (r['state'] && typeof r['state'] === 'object' ? r['state'] : raw) as Record<string, unknown>
  const name = String(r['name'] ?? st['drama'] ?? (st['name'] as string) ?? fallbackName)
  const clipsRaw = r['clips'] ?? st['clips'] ?? []
  const clips: ClipStateDTO[] = []
  if (Array.isArray(clipsRaw)) {
    for (const c of clipsRaw as Array<Record<string, unknown>>) {
      if (!c || typeof c !== 'object' || !c['id']) continue
      clips.push({
        id: String(c['id']), image: asStage(c['image']), video: asStage(c['video']),
        tts: asStage(c['tts']), mux: asStage(c['mux']),
        progress: typeof c['progress'] === 'number' ? c['progress'] : undefined,
        message: typeof c['message'] === 'string' ? c['message'] : undefined,
      })
    }
  } else if (clipsRaw && typeof clipsRaw === 'object') {
    // orchestrator 形：{s01:{image,video,tts,mux}}
    for (const [id, stages] of Object.entries(clipsRaw as Record<string, unknown>)) {
      const s = (stages && typeof stages === 'object' ? stages : {}) as Record<string, unknown>
      clips.push({
        id, image: asStage(s['image']), video: asStage(s['video']),
        tts: asStage(s['tts']), mux: asStage(s['mux']),
      })
    }
  }
  const finalsRaw = r['finals'] ?? st['finals']
  const existsRaw = r['final_exists'] ?? st['final_exists']
  const muxRaw = r['mux_detail'] ?? st['mux_detail']
  const updatedRaw = st['updated_at'] ?? r['updated_at']
  const dto: DramaStateDTO = { name, clips, total_seconds: typeof st['total_seconds'] === 'number' ? (st['total_seconds'] as number) : undefined }
  const finals = asFinals(finalsRaw)
  if (finals !== undefined) dto.finals = finals
  if (typeof existsRaw === 'boolean') dto.final_exists = existsRaw
  const mux = asMuxDetail(muxRaw)
  if (mux !== undefined) dto.mux_detail = mux
  if (typeof updatedRaw === 'string') dto.updated_at = updatedRaw
  return dto
}

/** POST /api/drama/{name}/retry {clip_id}：单镜重试（后端复位状态并自动开跑该镜）。 */
export function retryClipRequest(name: string, clipId: string): Promise<unknown> {
  return fetchJson<unknown>(`${QUEUE_API_BASE}/api/drama/${encodeURIComponent(name)}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clip_id: clipId })
  })
}

export function buildQueueStreamUrl(name?: string): string {
  const base = `${QUEUE_API_BASE}/api/queue/stream`
  return name ? `${base}?name=${encodeURIComponent(name)}` : base
}

/** POST /api/drama/{name}/start：开始渲染（全剧；重复点击后端 409）。 */
export function startRenderRequest(name: string): Promise<{ note?: string }> {
  return fetchJson<{ ok?: boolean; note?: string }>(
    `${QUEUE_API_BASE}/api/drama/${encodeURIComponent(name)}/start`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    },
    30000
  )
}

/** GET /api/drama/{name}/render-status：是否在跑（运行指示用）。 */
export async function fetchRenderStatus(name: string): Promise<boolean> {
  try {
    const dto = await fetchJson<{ running?: boolean }>(
      `${QUEUE_API_BASE}/api/drama/${encodeURIComponent(name)}/render-status`
    )
    return Boolean(dto.running)
  } catch {
    return false
  }
}

export interface QueueStreamHandlers {
  onEvent: (ev: QueueEventDTO) => void
  onError?: () => void
}

/**
 * 打开 SSE（GET /api/queue/stream），返回关闭函数。
 * 同时监听 message + 具名事件（clip/log/state/progress），data 既接受 JSON 也接受纯文本（降级为 log）。
 * 注意：调用方在 onerror 后应主动 close 并按指数退避重建（关闭 EventSource 原生自动重连，
 * 改由 store 层统一调度退避），见 stores/queue.ts。
 */
export function openQueueStream(
  name: string | undefined,
  handlers: QueueStreamHandlers
): () => void {
  const url = buildQueueStreamUrl(name)
  let closed = false
  let es: EventSource | null = null

  const handle = (raw: unknown) => {
    if (closed) return
    const text = String(raw ?? '').trim()
    if (!text) return
    try {
      const parsed = JSON.parse(text) as unknown as QueueEventDTO & { log?: unknown; data?: unknown }
      // 后端 SSE 形：{state:{clips:{...}}, log:[...], name, clips:[...]}，无 type
      if (parsed && typeof parsed === 'object' && !parsed.type && (parsed.state || parsed.clips || parsed.log)) {
        if (parsed.clips || parsed.state) handlers.onEvent(parsed as QueueEventDTO)
        const logs = (parsed as unknown as Record<string, unknown>)['log']
        if (Array.isArray(logs)) {
          for (const line of logs.slice(-5)) {
            if (typeof line === 'string' && line.trim())
              handlers.onEvent({ type: 'log', level: 'info', message: line })
          }
        }
        return
      }
      handlers.onEvent(parsed as QueueEventDTO)
    } catch {
      handlers.onEvent({ type: 'log', level: 'info', message: text })
    }
  }

  try {
    es = new EventSource(url)
  } catch {
    handlers.onError?.()
    return () => {
      closed = true
    }
  }

  const listener = (e: Event) => handle((e as MessageEvent).data)
  es.onmessage = listener
  for (const ch of ['clip', 'log', 'state', 'progress']) {
    try {
      es.addEventListener(ch, listener as EventListener)
    } catch {
      /* 旧浏览器容错：仅保留 onmessage */
    }
  }
  es.onerror = () => {
    if (!closed) handlers.onError?.()
  }
  return () => {
    closed = true
    try {
      es?.close()
    } catch {
      /* 关闭幂等，忽略 */
    }
    es = null
  }
}
