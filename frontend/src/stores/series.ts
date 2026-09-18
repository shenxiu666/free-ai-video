/**
 * Builder M2 · 系列 pinia store。
 * state 严格保持 { seriesList, currentSeries, episodes, characters } 四字段（外加 loading/error 辅助）。
 * 缺后端时：读/写一律落 localStorage 本地降级，不抛阻断；后端明确拒绝才上抛。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  listSeries,
  createSeries,
  fetchSeries,
  listEpisodes,
  createEpisode,
  uploadEpisodeFile,
  listCharacters,
  createCharacter,
  isSeriesOfflineError,
  episodeDramaKey,
  type SeriesSummary,
  type SeriesDetailDTO,
  type EpisodeSummary,
  type SeriesCharacter
} from '../api/series'

const LS_INDEX = 'free-ai-video:series:index'
const LS_SERIES_PREFIX = 'free-ai-video:series:'
const LS_EPS_PREFIX = 'free-ai-video:episodes:'
const LS_CHARS_PREFIX = 'free-ai-video:characters:'

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* 配额不足仅保留内存 */
  }
}

function lsDel(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* 忽略 */
  }
}

function readJson<T>(key: string, fallback: T): T {
  const raw = lsGet(key)
  if (!raw) return fallback
  try {
    return JSON.parse(raw) as T
  } catch {
    return fallback
  }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function countWords(s: string): number {
  return [...s.trim()].filter((c) => c.trim().length > 0).length
}

/** 浅合并同一 id 的两组列表：incoming 为 undefined 的键不覆盖，保留 drama_name/style_override 等关键键。 */
function mergeEpisodeLists(a: EpisodeSummary[], b: EpisodeSummary[]): EpisodeSummary[] {
  const map = new Map<string, EpisodeSummary>()
  for (const e of [...a, ...b]) {
    const prev = map.get(e.id)
    if (!prev) {
      map.set(e.id, { ...e })
    } else {
      const merged: EpisodeSummary = { ...prev }
      for (const [k, v] of Object.entries(e as unknown as Record<string, unknown>)) {
        if (v !== undefined) (merged as unknown as Record<string, unknown>)[k] = v
      }
      map.set(e.id, merged)
    }
  }
  return [...map.values()]
}

/** 浅合并角色列表：incoming 为 undefined 的键不覆盖，保留 portrait_url/is_main/relation/outfit/status 等关键键。 */
function mergeCharacterLists(a: SeriesCharacter[], b: SeriesCharacter[]): SeriesCharacter[] {
  const map = new Map<string, SeriesCharacter>()
  for (const c of [...a, ...b]) {
    const prev = map.get(c.id)
    if (!prev) {
      map.set(c.id, { ...c })
    } else {
      const merged: SeriesCharacter = { ...prev }
      for (const [k, v] of Object.entries(c as unknown as Record<string, unknown>)) {
        if (v !== undefined) (merged as unknown as Record<string, unknown>)[k] = v
      }
      map.set(c.id, merged)
    }
  }
  return [...map.values()]
}

/** 最近一次 extract 结果（供角色页空态三区分用）：null=从未识别；ok=false=上次失败；ok=true&count=0=识别为0。 */
export interface LastExtractState {
  ok: boolean
  count: number
  skipped: number
  error: string
  time: number
  seriesId: string
}

export const useSeriesStore = defineStore('series', () => {
  const seriesList = ref<SeriesSummary[]>([])
  const currentSeries = ref<SeriesDetailDTO | null>(null)
  const episodes = ref<EpisodeSummary[]>([])
  const characters = ref<SeriesCharacter[]>([])
  const loading = ref(false)
  const error = ref('')
  /** 最近一次读取来源：remote / local / '' */
  const lastSource = ref<'' | 'remote' | 'local'>('')
  const lastExtract = ref<LastExtractState | null>(null)

  function setLastExtract(payload: { ok: boolean; count?: number; skipped?: number; error?: string; seriesId?: string }): void {
    const sid = typeof payload.seriesId === 'string' && payload.seriesId.trim()
      ? payload.seriesId.trim()
      : (currentSeries.value?.id ?? '')
    lastExtract.value = {
      ok: payload.ok,
      count: typeof payload.count === 'number' && Number.isFinite(payload.count) ? payload.count : 0,
      skipped: typeof payload.skipped === 'number' && Number.isFinite(payload.skipped) ? payload.skipped : 0,
      error: typeof payload.error === 'string' ? payload.error : '',
      time: Date.now(),
      seriesId: sid
    }
  }

  function clearLastExtract(): void {
    lastExtract.value = null
  }

  function persistIndex(): void {
    lsSet(LS_INDEX, JSON.stringify(seriesList.value))
  }

  function persistSeriesScope(seriesId: string): void {
    if (currentSeries.value) lsSet(LS_SERIES_PREFIX + seriesId, JSON.stringify(currentSeries.value))
    lsSet(LS_EPS_PREFIX + seriesId, JSON.stringify(episodes.value))
    lsSet(LS_CHARS_PREFIX + seriesId, JSON.stringify(characters.value))
  }

  /** 加载系列列表：无后端时降级本地索引。 */
  async function loadSeriesList(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const remote = await listSeries()
      seriesList.value = remote
      lastSource.value = 'remote'
      persistIndex()
    } catch (e) {
      const local = readJson<SeriesSummary[]>(LS_INDEX, [])
      seriesList.value = local
      lastSource.value = 'local'
      if (!isSeriesOfflineError(e)) error.value = errMsg(e)
    } finally {
      loading.value = false
    }
  }

  /** 新建系列：无后端时本地建（id = title），照常可进详情/加集。 */
  async function newSeries(payload: { title: string; synopsis?: string; style?: string }): Promise<SeriesSummary> {
    const title = payload.title.trim()
    if (!title) throw new Error('请填写系列名')
    loading.value = true
    error.value = ''
    try {
      const created = await createSeries({ title, synopsis: payload.synopsis ?? '', style: payload.style ?? '' })
      if (!seriesList.value.some((s) => s.id === created.id)) seriesList.value.push(created)
      persistIndex()
      lastSource.value = 'remote'
      return created
    } catch (e) {
      if (!isSeriesOfflineError(e)) throw e
      const local: SeriesSummary = { id: title, title, synopsis: payload.synopsis ?? '', style: payload.style ?? '' }
      if (!seriesList.value.some((s) => s.id === local.id)) seriesList.value.push(local)
      persistIndex()
      lastSource.value = 'local'
      return local
    } finally {
      loading.value = false
    }
  }

  /** 选中系列：详情 + 集 + 角色并行拉取，任一离线即整组走本地。 */
  async function selectSeries(seriesId: string): Promise<void> {
    const id = seriesId.trim()
    if (!id) throw new Error('系列 id 为空')
    loading.value = true
    error.value = ''
    try {
      const [detail, eps, chars] = await Promise.all([
        fetchSeries(id).catch(() => null),
        listEpisodes(id).catch(() => null),
        listCharacters(id).catch(() => null)
      ])
      if (detail || eps || chars) {
        const known = seriesList.value.find((s) => s.id === id)
        currentSeries.value = detail ?? {
          id,
          title: known?.title ?? id,
          synopsis: known?.synopsis,
          style: known?.style
        }
        const detailEps = detail?.episodes ?? []
        const detailChars = detail?.characters ?? []
        if (eps && detailEps.length) episodes.value = mergeEpisodeLists(detailEps, eps)
        else episodes.value = eps ?? (detailEps.length ? detailEps : readJson<EpisodeSummary[]>(LS_EPS_PREFIX + id, []))
        if (chars && detailChars.length) characters.value = mergeCharacterLists(detailChars, chars)
        else characters.value = chars ?? (detailChars.length ? detailChars : readJson<SeriesCharacter[]>(LS_CHARS_PREFIX + id, []))
        lastSource.value = 'remote'
        persistSeriesScope(id)
        return
      }
      throw new Error('无法连接后端：请确认后端已在 http://localhost:8000 启动')
    } catch (e) {
      // 本地降级：索引 + 系列域缓存
      const known = seriesList.value.find((s) => s.id === id)
        ?? readJson<SeriesSummary[]>(LS_INDEX, []).find((s) => s.id === id)
      currentSeries.value = readJson<SeriesDetailDTO | null>(LS_SERIES_PREFIX + id, null)
        ?? (known ? { id, title: known.title, synopsis: known.synopsis, style: known.style } : { id, title: id })
      episodes.value = readJson<EpisodeSummary[]>(LS_EPS_PREFIX + id, [])
      characters.value = readJson<SeriesCharacter[]>(LS_CHARS_PREFIX + id, [])
      lastSource.value = 'local'
      if (!isSeriesOfflineError(e)) error.value = errMsg(e)
    } finally {
      loading.value = false
    }
  }

  /** 加一集（粘贴）：无后端时本地建（字数/风格本地算，不发请求）。total_seconds 可选，undefined 时不传该键。 */
  async function addEpisode(payload: { title: string; source_text?: string; style_override?: string; total_seconds?: number }): Promise<EpisodeSummary> {
    const sid = currentSeries.value?.id ?? ''
    if (!sid) throw new Error('请先选择系列')
    const title = payload.title.trim() || `第${episodes.value.length + 1}集`
    try {
      const created = await createEpisode(sid, { ...payload, title })
      episodes.value.push(created)
      persistSeriesScope(sid)
      return created
    } catch (e) {
      if (!isSeriesOfflineError(e)) throw e
      const local: EpisodeSummary = {
        id: `${Date.now()}`,
        series_id: sid,
        title,
        drama_name: title,
        word_count: countWords(payload.source_text ?? ''),
        ...(payload.total_seconds !== undefined
          ? { planned_seconds: payload.total_seconds, total_seconds: payload.total_seconds }
          : {}),
        style_override: payload.style_override ?? '',
        status: 'local'
      }
      episodes.value.push(local)
      persistSeriesScope(sid)
      return local
    }
  }

  /** 加一集（文件 .md/.txt）：无后端时本地读文件建集。total_seconds 可选，undefined 时不传该键。 */
  async function addEpisodeFile(
    file: File,
    opts: { title?: string; style_override?: string; total_seconds?: number } = {}
  ): Promise<EpisodeSummary> {
    const sid = currentSeries.value?.id ?? ''
    if (!sid) throw new Error('请先选择系列')
    if (!/\.(md|txt)$/i.test(file.name)) throw new Error('仅支持 .md / .txt 文件')
    try {
      const created = await uploadEpisodeFile(sid, file, opts)
      episodes.value.push(created)
      persistSeriesScope(sid)
      return created
    } catch (e) {
      if (!isSeriesOfflineError(e)) throw e
      const text = await file.text().catch(() => '')
      const title = (opts.title ?? '').trim() || file.name.replace(/\.(md|txt)$/i, '')
      const local: EpisodeSummary = {
        id: `${Date.now()}`,
        series_id: sid,
        title,
        drama_name: title,
        word_count: countWords(text),
        ...(opts.total_seconds !== undefined
          ? { planned_seconds: opts.total_seconds, total_seconds: opts.total_seconds }
          : {}),
        style_override: opts.style_override ?? '',
        status: 'local'
      }
      // 原文太大不进 localStorage，只记字数；原文由调用方自行进入分镜流程
      episodes.value.push(local)
      persistSeriesScope(sid)
      return local
    }
  }

  /** plan 回写 total 后同步 episodes 列表对应项（与 merge 一致：只覆盖 planned/total，不动其他键）。 */
  function applyEpisodeTotal(epId: string, total: number): void {
    const sid = currentSeries.value?.id ?? ''
    if (!sid || !Number.isFinite(total)) return
    const t = Math.round(total * 1000) / 1000
    episodes.value = episodes.value.map((e) =>
      e.id === epId ? { ...e, planned_seconds: t, total_seconds: t } : e
    )
    persistSeriesScope(sid)
  }

  /** 新建角色：无后端时本地建。 */
  async function addCharacter(payload: { name: string; desc?: string }): Promise<SeriesCharacter> {
    const sid = currentSeries.value?.id ?? ''
    if (!sid) throw new Error('请先选择系列')
    const name = payload.name.trim()
    if (!name) throw new Error('请填写角色名')
    try {
      const created = await createCharacter(sid, { name, desc: payload.desc ?? '' })
      characters.value.push(created)
      persistSeriesScope(sid)
      return created
    } catch (e) {
      if (!isSeriesOfflineError(e)) throw e
      const local: SeriesCharacter = { id: `${Date.now()}`, name, desc: payload.desc ?? '' }
      characters.value.push(local)
      persistSeriesScope(sid)
      return local
    }
  }

  function removeLocalCharacter(charId: string): void {
    characters.value = characters.value.filter((c) => c.id !== charId)
    const sid = currentSeries.value?.id
    if (sid) persistSeriesScope(sid)
  }

  function removeLocalSeries(seriesId: string): void {
    seriesList.value = seriesList.value.filter((s) => s.id !== seriesId)
    persistIndex()
    lsDel(LS_SERIES_PREFIX + seriesId)
    lsDel(LS_EPS_PREFIX + seriesId)
    lsDel(LS_CHARS_PREFIX + seriesId)
    if (currentSeries.value?.id === seriesId) {
      currentSeries.value = null
      episodes.value = []
      characters.value = []
    }
  }

  return {
    seriesList, currentSeries, episodes, characters, loading, error, lastSource,
    lastExtract, setLastExtract, clearLastExtract,
    loadSeriesList, newSeries, selectSeries, addEpisode, addEpisodeFile, addCharacter,
    removeLocalCharacter, removeLocalSeries, applyEpisodeTotal, episodeDramaKey
  }
})
