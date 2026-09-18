/**
 * Builder M2 · 系列 / 集 / 角色 REST 封装。
 *
 * 基址固定 http://localhost:8000；失败一律抛中文错误。
 * 后端系列接口（与后端 M1 约定，缺接口时前端降级本地索引，不崩）：
 *   GET    /api/series                                   系列列表
 *   POST   /api/series                                   新建系列 {title, synopsis?, style?}
 *   GET    /api/series/{seriesId}                        系列详情（含 episodes/characters 可选内嵌）
 *   GET    /api/series/{seriesId}/episodes               集列表
 *   POST   /api/series/{seriesId}/episodes               加一集 {title, source_text?, style_override?, total_seconds?}
 *   POST   /api/series/{seriesId}/episodes/upload        加一集（文件 .md/.txt，FormData: file + title? + style_override?）
 *   POST   /api/series/{seriesId}/episodes/{epId}/plan       AI 规划预览（hook/悬念/风险，不落盘）
 *   POST   /api/series/{seriesId}/episodes/{epId}/breakdown  AI 分镜初稿（超时 180s，不落盘）
 *   POST   /api/series/{seriesId}/characters/extract        按分集识别角色 {episode_id}
 *   GET    /api/series/{seriesId}/characters             角色列表
 *   POST   /api/series/{seriesId}/characters             新建角色 {name, desc?}
 *   PATCH  /api/series/{seriesId}/characters/{charId}    更新角色（含设主图 is_main/portrait_url）
 *   DELETE /api/series/{seriesId}/characters/{charId}    删除角色
 *   POST   /api/series/{seriesId}/characters/merge       合并角色 {source_ids, target_id}
 *   POST   /api/series/{seriesId}/characters/{charId}/portrait  生成立绘 {style?}
 *   POST   /api/series/{seriesId}/characters/{charId}/images    上传立绘（FormData: file，超时 180s）
 *
 * 安全：前端只见掩码与立绘 URL，KEK / Key 明文绝不经此模块。
 */

export const SERIES_API_BASE = 'http://localhost:8000'
export const SERIES_TIMEOUT_MS = 15_000
export const SERIES_BREAKDOWN_TIMEOUT_MS = 180_000

export interface SeriesSummary {
  id: string
  title: string
  synopsis?: string
  style?: string
  episode_count?: number
  created_at?: string
}

export interface EpisodeSummary {
  /** 集 id（队列 key 优先用 drama_name，其次 title，最后 id） */
  id: string
  series_id?: string
  title: string
  /** 映射到老单剧流水线的剧名：后端若未下发，前端用 title 回填 */
  drama_name?: string
  word_count?: number
  planned_seconds?: number
  /** 后端 plan 回写后的分集总时长（与 planned_seconds 同值，兼容两种键名） */
  total_seconds?: number
  style_override?: string
  status?: string
}

export interface SeriesCharacter {
  id: string
  name: string
  desc?: string
  /** 别名（后端数组；逗号分隔输入，前端展示“又名：…”） */
  aliases?: string[]
  /** 外貌（≤200字，由识别/手工维护） */
  appearance?: string
  /** 性格 */
  personality?: string
  /** 备注（后端 canonical 键 notes；note 为旧前端别名，仅读兼容） */
  notes?: string
  /** @deprecated 读兼容别名：写一律用 notes，后端 PATCH 白名单仅收 notes */
  note?: string
  portrait_url?: string
  is_main?: boolean
  /** 用户手工编辑后锁定：自动识别不再覆盖 */
  locked?: boolean
  updated_at?: string
  relation?: string
  outfit?: string
  /** 仅收“待确认”/“已确认”，其他一律归一为 undefined */
  status?: string
}

export interface SeriesDetailDTO {
  id: string
  title: string
  synopsis?: string
  style?: string
  episodes?: EpisodeSummary[]
  characters?: SeriesCharacter[]
}

export interface PlanPreview {
  hook?: string
  cliffhanger?: string
  risks?: string[]
  estimated_seconds?: number
  beats?: string[]
  clip_durations?: number[]
  density?: { chars_per_sec?: number; dialogue_max?: number }
  style_effective?: string
  total_seconds?: number
  /** 时长来源：AI 规划 / 手动填写（后端 total_source，大小写容错） */
  total_source?: string
  [key: string]: unknown
}

export interface SeriesMissingAsset {
  clip_id: string
  field: string
  kind: string
  name: string
  planned_path: string
}

export interface SeriesBreakdownResult {
  ok?: boolean
  script?: unknown
  generated_by?: string
  note?: string
  missing_assets?: SeriesMissingAsset[]
}

/** 系列场景（含后端派生的 image_url 主图直链）。 */
export interface SeriesScene {
  id: string
  name: string
  description: string
  images: string[]
  first_seen: string
  notes: string
  locked: boolean
  image_url: string
  [key: string]: unknown
}

function normScene(r: Record<string, unknown>): SeriesScene {
  const strList = (v: unknown): string[] =>
    Array.isArray(v) ? (v as unknown[]).filter((x): x is string => typeof x === 'string') : []
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')
  return {
    ...r,
    id: str(r['id']),
    name: str(r['name']),
    description: str(r['description']),
    images: strList(r['images']),
    first_seen: str(r['first_seen']),
    notes: str(r['notes']),
    locked: r['locked'] === true,
    image_url: str(r['image_url']),
  }
}

/** GET /api/series/{id}/scenes 场景列表。 */
export async function listScenes(seriesId: string): Promise<SeriesScene[]> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/scenes`)
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const arr = Array.isArray(rec['scenes']) ? (rec['scenes'] as unknown[]) : []
  return arr
    .filter((x): x is Record<string, unknown> => !!x && typeof x === 'object' && !Array.isArray(x))
    .map(normScene)
    .filter((c) => !!c.id)
}

/** POST /api/series/{id}/scenes 新建场景。 */
export function createScene(
  seriesId: string,
  payload: { name: string; description?: string; first_seen?: string; notes?: string }
): Promise<unknown> {
  return requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/scenes`, {
    method: 'POST',
    body: JSON.stringify(payload)
  })
}

/** POST /api/series/{id}/scenes/{sceneId}/image AI 生成场景图（走 Agnes 图模型，超时 180s）。 */
export function generateSceneImage(
  seriesId: string,
  sceneId: string,
  payload: { style?: string } = {}
): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(
    `/api/series/${encodeURIComponent(seriesId)}/scenes/${encodeURIComponent(sceneId)}/image`,
    { method: 'POST', body: JSON.stringify({ style: payload.style ?? '' }) },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

/** POST /api/series/{id}/scenes/{sceneId}/images 上传场景图（multipart FormData(file)，超时 180s）。 */
export function uploadSceneImage(
  seriesId: string,
  sceneId: string,
  file: File
): Promise<Record<string, unknown>> {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return requestJson<Record<string, unknown>>(
    `/api/series/${encodeURIComponent(seriesId)}/scenes/${encodeURIComponent(sceneId)}/images`,
    { method: 'POST', body: fd },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

function toChineseError(e: unknown, path: string): Error {
  if (e instanceof Error) {
    if (e.name === 'AbortError')
      return new Error(`请求超时：${path}，请确认后端已在 http://localhost:8000 启动`)
    const m = e.message || ''
    if (m.includes('Failed to fetch') || m.includes('NetworkError') || m.includes('Load failed') || m.includes('Network request failed'))
      return new Error(`无法连接后端（${path}）：请确认后端已在 http://localhost:8000 启动`)
    return e
  }
  return new Error(`请求失败（${path}）：未知错误`)
}

async function requestJson<T>(path: string, init: RequestInit = {}, timeoutMs = SERIES_TIMEOUT_MS): Promise<T> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const isForm = init.body instanceof FormData
    const res = await fetch(`${SERIES_API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: isForm
        ? { ...(init.headers || {}) }
        : { 'Content-Type': 'application/json', ...(init.headers || {}) }
    })
    if (!res.ok) {
      let detail = ''
      try {
        detail = (await res.text()).slice(0, 300)
      } catch {
        detail = ''
      }
      throw new Error(`后端返回 ${res.status}（${path}）：${detail || res.statusText || '无详情'}`)
    }
    if (res.status === 204) return undefined as T
    const text = await res.text()
    if (!text) return undefined as T
    try {
      return JSON.parse(text) as T
    } catch {
      throw new Error(`后端返回的不是合法 JSON（${path}），请检查后端日志`)
    }
  } catch (e) {
    throw toChineseError(e, path)
  } finally {
    window.clearTimeout(timer)
  }
}

/** 连不上后端（相对 4xx/5xx 明确拒绝而言的真·不可达），调用方据此走本地降级。 */
export function isSeriesOfflineError(e: unknown): boolean {
  const m = e instanceof Error ? e.message : String(e ?? '')
  return m.includes('无法连接后端') || m.includes('请求超时') || m.includes('Failed to fetch')
}

function asStr(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

function normSeries(r: Record<string, unknown>): SeriesSummary {
  return {
    id: asStr(r['id'] ?? r['series_id'] ?? r['title']),
    title: asStr(r['title'] ?? r['name'] ?? r['id']),
    synopsis: typeof r['synopsis'] === 'string' ? (r['synopsis'] as string) : undefined,
    style: typeof r['style'] === 'string' ? (r['style'] as string) : undefined,
    episode_count: typeof r['episode_count'] === 'number' ? (r['episode_count'] as number) : undefined,
    created_at: typeof r['created_at'] === 'string' ? (r['created_at'] as string) : undefined
  }
}

function normEpisode(r: Record<string, unknown>, seriesId = ''): EpisodeSummary {
  const title = asStr(r['title'] ?? r['name'] ?? r['id'] ?? r['drama_name'])
  const planned = typeof r['planned_seconds'] === 'number' ? (r['planned_seconds'] as number)
    : typeof r['total_seconds'] === 'number' ? (r['total_seconds'] as number) : undefined
  const total = typeof r['total_seconds'] === 'number' ? (r['total_seconds'] as number)
    : typeof r['planned_seconds'] === 'number' ? (r['planned_seconds'] as number) : undefined
  return {
    id: asStr(r['id'] ?? r['episode_id'] ?? title),
    series_id: asStr(r['series_id'] ?? seriesId),
    title,
    drama_name: typeof r['drama_name'] === 'string' ? (r['drama_name'] as string) : title,
    word_count: typeof r['word_count'] === 'number' ? (r['word_count'] as number) : undefined,
    planned_seconds: planned,
    total_seconds: total,
    style_override: typeof r['style_override'] === 'string' ? (r['style_override'] as string)
      : typeof r['style'] === 'string' ? (r['style'] as string) : undefined,
    status: typeof r['status'] === 'string' ? (r['status'] as string) : undefined
  }
}

function normCharacter(r: Record<string, unknown>): SeriesCharacter {
  const desc = typeof r['desc'] === 'string' && (r['desc'] as string)
    ? (r['desc'] as string)
    : typeof r['logline'] === 'string' && (r['logline'] as string)
      ? (r['logline'] as string)
      : typeof r['description'] === 'string' ? (r['description'] as string) : undefined
  let portrait: string | undefined
  if (typeof r['portrait_url'] === 'string' && (r['portrait_url'] as string)) {
    portrait = r['portrait_url'] as string
  } else if (Array.isArray(r['images']) && typeof (r['images'] as unknown[])[0] === 'string') {
    const s = (r['images'] as unknown[])[0] as string
    if (s.startsWith('http') || s.startsWith('file') || s.startsWith('data') || s.startsWith('/')) portrait = s
  } else if (typeof r['image'] === 'string' && (r['image'] as string)) {
    const s = r['image'] as string
    if (s.startsWith('http') || s.startsWith('file') || s.startsWith('data') || s.startsWith('/')) portrait = s
  }
  const relation = typeof r['relation'] === 'string' && (r['relation'] as string).trim()
    ? (r['relation'] as string).trim() : undefined
  const outfit = typeof r['outfit'] === 'string' && (r['outfit'] as string).trim()
    ? (r['outfit'] as string).trim() : undefined
  const rawStatus = typeof r['status'] === 'string' ? (r['status'] as string).trim() : ''
  const status = rawStatus === '待确认' || rawStatus === '已确认' ? rawStatus : undefined
  const aliasesRaw = r['aliases']
  let aliases: string[] | undefined
  if (Array.isArray(aliasesRaw)) {
    const list = (aliasesRaw as unknown[]).filter((x): x is string => typeof x === 'string').map((s) => s.trim()).filter((s) => s.length > 0)
    if (list.length) aliases = list
  } else if (typeof aliasesRaw === 'string' && aliasesRaw.trim()) {
    const list = aliasesRaw.split(/[,，、;；]/).map((s) => s.trim()).filter((s) => s.length > 0)
    if (list.length) aliases = list
  }
  const appearance = typeof r['appearance'] === 'string' && (r['appearance'] as string).trim()
    ? (r['appearance'] as string).trim() : undefined
  const personality = typeof r['personality'] === 'string' && (r['personality'] as string).trim()
    ? (r['personality'] as string).trim() : undefined
  const noteRaw = typeof r['note'] === 'string' && (r['note'] as string).trim()
    ? (r['note'] as string).trim()
    : typeof r['notes'] === 'string' && (r['notes'] as string).trim()
      ? (r['notes'] as string).trim()
      : typeof r['remark'] === 'string' && (r['remark'] as string).trim()
        ? (r['remark'] as string).trim() : undefined
  const locked = typeof r['locked'] === 'boolean' ? (r['locked'] as boolean) : undefined
  return {
    id: asStr(r['id'] ?? r['character_id'] ?? r['name']),
    name: asStr(r['name'] ?? r['id']),
    desc,
    ...(aliases !== undefined ? { aliases } : {}),
    ...(appearance !== undefined ? { appearance } : {}),
    ...(personality !== undefined ? { personality } : {}),
    ...(noteRaw !== undefined ? { note: noteRaw, notes: noteRaw } : {}),
    portrait_url: portrait,
    is_main: typeof r['is_main'] === 'boolean' ? (r['is_main'] as boolean) : undefined,
    ...(locked !== undefined ? { locked } : {}),
    updated_at: typeof r['updated_at'] === 'string' ? (r['updated_at'] as string) : undefined,
    ...(relation !== undefined ? { relation } : {}),
    ...(outfit !== undefined ? { outfit } : {}),
    ...(status !== undefined ? { status } : {})
  }
}

function pickList(raw: unknown): Record<string, unknown>[] {
  if (Array.isArray(raw)) return raw.filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
  if (raw && typeof raw === 'object') {
    const r = raw as Record<string, unknown>
    for (const k of ['series', 'items', 'data', 'episodes', 'characters', 'list']) {
      if (Array.isArray(r[k])) return (r[k] as unknown[]).filter((x): x is Record<string, unknown> => !!x && typeof x === 'object')
    }
  }
  return []
}

/** 集级的老单剧 key：队列复用 fetchDramaState/retry/start 时用它（episode 级）。 */
export function episodeDramaKey(ep: Pick<EpisodeSummary, 'drama_name' | 'title' | 'id'>): string {
  return (ep.drama_name || '').trim() || (ep.title || '').trim() || (ep.id || '').trim()
}

/** GET /api/series 系列列表（兼容裸数组与 {series/items/data} 包络）。 */
export async function listSeries(): Promise<SeriesSummary[]> {
  const raw = await requestJson<unknown>('/api/series')
  return pickList(raw).map(normSeries).filter((s) => !!s.id)
}

export interface CreateSeriesPayload {
  title: string
  synopsis?: string
  style?: string
}

/** POST /api/series 新建系列。 */
export async function createSeries(payload: CreateSeriesPayload): Promise<SeriesSummary> {
  const raw = await requestJson<unknown>('/api/series', {
    method: 'POST',
    body: JSON.stringify({ title: payload.title.trim(), synopsis: payload.synopsis ?? '', style: payload.style ?? '' })
  })
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['series'] && typeof rec['series'] === 'object' ? rec['series'] : raw) as Record<string, unknown>
  const norm = normSeries((inner && typeof inner === 'object' ? inner : rec) as Record<string, unknown>)
  if (!norm.id) return { id: payload.title.trim(), title: payload.title.trim(), synopsis: payload.synopsis, style: payload.style }
  return norm
}

/** GET /api/series/{id} 系列详情。 */
export async function fetchSeries(seriesId: string): Promise<SeriesDetailDTO> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}`)
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['series'] && typeof rec['series'] === 'object' ? rec['series'] : rec) as Record<string, unknown>
  const base = normSeries(inner)
  const epRaw = rec['episodes'] ?? inner['episodes']
  const chRaw = rec['characters'] ?? inner['characters']
  return {
    id: base.id || seriesId,
    title: base.title || seriesId,
    synopsis: base.synopsis,
    style: base.style,
    episodes: Array.isArray(epRaw) ? (epRaw as unknown[]).filter((x): x is Record<string, unknown> => !!x && typeof x === 'object').map((e) => normEpisode(e, base.id)) : undefined,
    characters: Array.isArray(chRaw) ? (chRaw as unknown[]).filter((x): x is Record<string, unknown> => !!x && typeof x === 'object').map(normCharacter) : undefined
  }
}

/** GET /api/series/{id}/episodes 集列表。 */
export async function listEpisodes(seriesId: string): Promise<EpisodeSummary[]> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/episodes`)
  return pickList(raw).map((e) => normEpisode(e, seriesId)).filter((e) => !!e.id)
}

export interface CreateEpisodePayload {
  title: string
  source_text?: string
  style_override?: string
  total_seconds?: number
}

/** POST /api/series/{id}/episodes 加一集（粘贴文本）。 */
export async function createEpisode(seriesId: string, payload: CreateEpisodePayload): Promise<EpisodeSummary> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/episodes`, {
    method: 'POST',
    body: JSON.stringify({
      title: payload.title.trim(),
      source_text: payload.source_text ?? '',
      style_override: payload.style_override ?? '',
      ...(typeof payload.total_seconds === 'number' ? { total_seconds: payload.total_seconds } : {})
    })
  })
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['episode'] && typeof rec['episode'] === 'object' ? rec['episode'] : raw) as Record<string, unknown>
  return normEpisode((inner && typeof inner === 'object' ? inner : { title: payload.title }) as Record<string, unknown>, seriesId)
}

/** POST /api/series/{id}/episodes/upload 加一集（.md/.txt 文件）。 */
export async function uploadEpisodeFile(
  seriesId: string,
  file: File,
  opts: { title?: string; style_override?: string; total_seconds?: number } = {}
): Promise<EpisodeSummary> {
  const fd = new FormData()
  fd.append('file', file, file.name)
  if (opts.title) fd.append('title', opts.title)
  if (opts.style_override) fd.append('style_override', opts.style_override)
  if (typeof opts.total_seconds === 'number') fd.append('total_seconds', String(opts.total_seconds))
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/episodes/upload`, {
    method: 'POST',
    body: fd
  })
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['episode'] && typeof rec['episode'] === 'object' ? rec['episode'] : raw) as Record<string, unknown>
  const fallback: Record<string, unknown> = { title: opts.title || file.name.replace(/\.(md|txt)$/i, '') }
  return normEpisode((inner && typeof inner === 'object' ? inner : fallback) as Record<string, unknown>, seriesId)
}

/** POST /api/series/{id}/episodes/{epId}/plan AI 规划预览（hook/悬念/风险，不落盘）。
 * body 可为 {}（total 省略即 AI 定时长）；响应 {ok, plan, total_source}，plan 落盘后分集 total 已回写。 */
export async function planEpisode(
  seriesId: string,
  epId: string,
  payload: { total_seconds?: number; style?: string; source_text?: string } = {}
): Promise<PlanPreview> {
  const body: Record<string, unknown> = {}
  if (typeof payload.total_seconds === 'number') body['total_seconds'] = payload.total_seconds
  if (typeof payload.style === 'string' && payload.style) body['style'] = payload.style
  if (typeof payload.source_text === 'string' && payload.source_text) body['source_text'] = payload.source_text
  const raw = await requestJson<unknown>(
    `/api/series/${encodeURIComponent(seriesId)}/episodes/${encodeURIComponent(epId)}/plan`,
    { method: 'POST', body: JSON.stringify(body) }
  )
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['plan'] && typeof rec['plan'] === 'object' ? rec['plan'] : rec) as Record<string, unknown>
  const market = (inner['market'] && typeof inner['market'] === 'object'
    ? inner['market'] as Record<string, unknown> : null)
  const toBeatStr = (v: unknown): string => {
    if (typeof v === 'string') return v
    if (v && typeof v === 'object') {
      try {
        return JSON.stringify(v).slice(0, 60)
      } catch {
        return String(v).slice(0, 60)
      }
    }
    return String(v)
  }
  const marketBeats = market && Array.isArray(market['beats'])
    ? (market['beats'] as unknown[]).map(toBeatStr) : undefined
  const fallbackBeats = Array.isArray(inner['beats'])
    ? (inner['beats'] as unknown[]).map(toBeatStr) : undefined
  const marketRisk = market ? market['risk'] : undefined
  const risks = market
    ? (typeof marketRisk === 'string' && marketRisk.trim() ? [marketRisk] : [])
    : Array.isArray(inner['risks'])
      ? (inner['risks'] as unknown[]).map(String).filter((s) => s.trim().length > 0) : undefined
  const totalSeconds = typeof inner['total_seconds'] === 'number'
    ? (inner['total_seconds'] as number) : undefined
  const clipRaw = inner['clip_durations']
  const clipDurations = Array.isArray(clipRaw)
    ? (clipRaw as unknown[]).filter((n): n is number => typeof n === 'number') : undefined
  const densityRaw = inner['density'] && typeof inner['density'] === 'object'
    ? inner['density'] as Record<string, unknown> : undefined
  const density = densityRaw
    ? {
        ...(typeof densityRaw['chars_per_sec'] === 'number' ? { chars_per_sec: densityRaw['chars_per_sec'] as number } : {}),
        ...(typeof densityRaw['dialogue_max'] === 'number' ? { dialogue_max: densityRaw['dialogue_max'] as number } : {})
      }
    : undefined
  const styleEffective = typeof inner['style_effective'] === 'string'
    ? (inner['style_effective'] as string) : undefined
  const totalSource = typeof rec['total_source'] === 'string' ? (rec['total_source'] as string)
    : typeof inner['total_source'] === 'string' ? (inner['total_source'] as string) : undefined
  return {
    ...inner,
    hook: market && typeof market['hook_3s'] === 'string' ? (market['hook_3s'] as string)
      : typeof inner['hook'] === 'string' ? (inner['hook'] as string) : undefined,
    cliffhanger: market && typeof market['cliffhanger'] === 'string' ? (market['cliffhanger'] as string)
      : typeof inner['cliffhanger'] === 'string' ? (inner['cliffhanger'] as string)
        : typeof inner['suspense'] === 'string' ? (inner['suspense'] as string) : undefined,
    risks,
    estimated_seconds: totalSeconds
      ?? (typeof inner['estimated_seconds'] === 'number' ? (inner['estimated_seconds'] as number)
        : typeof inner['planned_seconds'] === 'number' ? (inner['planned_seconds'] as number) : undefined),
    beats: marketBeats ?? fallbackBeats,
    clip_durations: clipDurations,
    density,
    style_effective: styleEffective,
    total_seconds: totalSeconds,
    ...(totalSource !== undefined ? { total_source: totalSource } : {})
  }
}

/** POST /api/series/{id}/episodes/{epId}/breakdown AI 分镜初稿（180s 超时）。
 * body 可为 {}（无 plan.json 时后端自动先规划，clip_seconds/style 均可选）。 */
export function breakdownEpisode(
  seriesId: string,
  epId: string,
  payload: { clip_seconds?: string; style?: string } = {}
): Promise<SeriesBreakdownResult> {
  const body: Record<string, string> = {}
  if (typeof payload.clip_seconds === 'string' && payload.clip_seconds.trim()) {
    body['clip_seconds'] = payload.clip_seconds.trim()
  }
  if (typeof payload.style === 'string' && payload.style.trim()) {
    body['style'] = payload.style.trim()
  }
  return requestJson<SeriesBreakdownResult>(
    `/api/series/${encodeURIComponent(seriesId)}/episodes/${encodeURIComponent(epId)}/breakdown`,
    { method: 'POST', body: JSON.stringify(body) },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

export interface EpisodeCharactersExtractResult {
  ok?: boolean
  characters?: unknown
  count?: number
  skipped?: number
  note?: string
  generated_by?: string
  [key: string]: unknown
}

/** POST /api/series/{id}/characters/extract 按分集识别角色（body {episode_id}）。 */
export function extractEpisodeCharacters(seriesId: string, epId: string): Promise<EpisodeCharactersExtractResult> {
  return requestJson<EpisodeCharactersExtractResult>(
    `/api/series/${encodeURIComponent(seriesId)}/characters/extract`,
    { method: 'POST', body: JSON.stringify({ episode_id: epId }) },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

/** 全文识别切片长度：绑定 backend/series.py:38 SOURCE_MAX_CHARS 与 backend/characters.py:207 截断值（12000字）。 */
export const NOVEL_CHUNK_CHARS = 12000

/** POST /api/series/{id}/characters/extract 全文直调（body {source_text, first_seen:'全文'}，180s 超时）。 */
export function extractNovelCharacters(seriesId: string, chunkText: string): Promise<EpisodeCharactersExtractResult> {
  return requestJson<EpisodeCharactersExtractResult>(
    `/api/series/${encodeURIComponent(seriesId)}/characters/extract`,
    { method: 'POST', body: JSON.stringify({ source_text: chunkText, first_seen: '全文' }) },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

/** GET /api/series/{id}/characters 角色列表。 */
export async function listCharacters(seriesId: string): Promise<SeriesCharacter[]> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/characters`)
  return pickList(raw).map(normCharacter).filter((c) => !!c.id)
}

/** POST /api/series/{id}/characters 新建角色。 */
export async function createCharacter(
  seriesId: string,
  payload: { name: string; desc?: string }
): Promise<SeriesCharacter> {
  const raw = await requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/characters`, {
    method: 'POST',
    body: JSON.stringify({ name: payload.name.trim(), desc: payload.desc ?? '' })
  })
  const rec = (raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const inner = (rec['character'] && typeof rec['character'] === 'object' ? rec['character'] : raw) as Record<string, unknown>
  return normCharacter((inner && typeof inner === 'object' ? inner : { name: payload.name }) as Record<string, unknown>)
}

/** PATCH /api/series/{id}/characters/{charId} 更新角色（含设主图/确认状态/卡片编辑全字段+locked）。 */
export function updateCharacter(
  seriesId: string,
  charId: string,
  patch: Partial<Pick<SeriesCharacter, 'name' | 'desc' | 'aliases' | 'appearance' | 'personality' | 'notes' | 'portrait_url' | 'is_main' | 'locked' | 'relation' | 'outfit' | 'status'>>
): Promise<unknown> {
  return requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/characters/${encodeURIComponent(charId)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch)
  })
}

/** DELETE /api/series/{id}/characters/{charId} 删除角色。 */
export function deleteCharacter(seriesId: string, charId: string): Promise<unknown> {
  return requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/characters/${encodeURIComponent(charId)}`, {
    method: 'DELETE'
  })
}

/** POST /api/series/{id}/characters/merge 合并角色（source_ids 并入 target_id）。 */
export function mergeCharacters(seriesId: string, payload: { source_ids: string[]; target_id: string }): Promise<unknown> {
  return requestJson<unknown>(`/api/series/${encodeURIComponent(seriesId)}/characters/merge`, {
    method: 'POST',
    body: JSON.stringify(payload)
  })
}

/** POST /api/series/{id}/characters/{charId}/portrait 生成立绘（走 Agnes 图模型，超时 180s）。
 * 后端回包形如 {ok, character, portrait_url?, path?}：character.portrait_url 由 _present_character 派生绝对 URL，优先读该层。 */
export function generatePortrait(
  seriesId: string,
  charId: string,
  payload: { style?: string } = {}
): Promise<{ portrait_url?: string; path?: string; character?: SeriesCharacter } & Record<string, unknown>> {
  return requestJson<{ portrait_url?: string } & Record<string, unknown>>(
    `/api/series/${encodeURIComponent(seriesId)}/characters/${encodeURIComponent(charId)}/portrait`,
    { method: 'POST', body: JSON.stringify({ style: payload.style ?? '' }) },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

/** POST /api/series/{id}/characters/{charId}/images 上传立绘（multipart FormData(file)，不手动设 Content-Type，超时 180s）。
 * 后端回包只返 {ok, character, path: 相对 rel}，无顶层 portrait_url：直链取 character.portrait_url（_present_character 已派生绝对 URL）。 */
export function uploadPortrait(
  seriesId: string,
  charId: string,
  file: File
): Promise<{ portrait_url?: string; path?: string; character?: SeriesCharacter } & Record<string, unknown>> {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return requestJson<{ portrait_url?: string; path?: string; character?: SeriesCharacter } & Record<string, unknown>>(
    `/api/series/${encodeURIComponent(seriesId)}/characters/${encodeURIComponent(charId)}/images`,
    { method: 'POST', body: fd },
    SERIES_BREAKDOWN_TIMEOUT_MS
  )
}

/** 立绘回包取直链：优先 character.portrait_url（后端 _present_character 已派生绝对 URL），回退顶层 portrait_url，再回退绝对形 path。相对 path 返回 ''。 */
export function resolvePortraitUrl(res: unknown): string {
  if (!res || typeof res !== 'object') return ''
  const r = res as Record<string, unknown>
  const ch = r['character']
  if (ch && typeof ch === 'object') {
    const u = (ch as Record<string, unknown>)['portrait_url']
    if (typeof u === 'string' && u) return u
  }
  const top = r['portrait_url']
  if (typeof top === 'string' && top) return top
  const p = r['path']
  if (typeof p === 'string' && p && (p.startsWith('http') || p.startsWith('file') || p.startsWith('data') || p.startsWith('/'))) return p
  return ''
}

/** 立绘回包取 path 原文（相对 rel 也原样返回，供“已存 …，刷新查看”提示用）。 */
export function resolvePortraitPath(res: unknown): string {
  if (!res || typeof res !== 'object') return ''
  const p = (res as Record<string, unknown>)['path']
  return typeof p === 'string' ? p : ''
}
