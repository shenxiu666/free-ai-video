import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  fetchDramaState,
  retryClipRequest,
  startRenderRequest,
  fetchRenderStatus,
  openQueueStream,
  QueueHttpError,
  type ClipStateDTO,
  type DramaStateDTO,
  type FinalEntryDTO,
  type MuxDetailDTO,
  type QueueEventDTO,
  type StageName,
  type StageStatus
} from '../api/queue'

export type { StageName, StageStatus }

export interface ClipState {
  id: string
  image: StageStatus
  video: StageStatus
  tts: StageStatus
  mux: StageStatus
  progress: number
  message: string
}

export interface LogEntry {
  time: string
  level: 'info' | 'warn' | 'error'
  msg: string
}

export interface FinalInfo {
  finals: FinalEntryDTO[]
  final_exists: boolean
  mux_detail: MuxDetailDTO | null
  updated_at: string
}

function emptyFinalInfo(): FinalInfo {
  return { finals: [], final_exists: false, mux_detail: null, updated_at: '' }
}

/** 每镜四阶段：分镜图 / 分镜视频 / 配音 / 合成（04 章 state.json 约定）。 */
export const STAGES: ReadonlyArray<{ key: StageName; label: string }> = [
  { key: 'image', label: '分镜图' },
  { key: 'video', label: '分镜视频' },
  { key: 'tts', label: '配音' },
  { key: 'mux', label: '合成' }
]

const STAGE_KEYS: StageName[] = ['image', 'video', 'tts', 'mux']
const MAX_LOGS = 500
const POLL_MS = 30000 // SSE 主推 + 30s 轮询兜底（02.9）
const FAKE_MS = 1500 // 无后端演示进度节拍
const MAX_BACKOFF_MS = 30000 // 指数退避上限

function normalizeStage(s: unknown): StageStatus {
  return s === 'doing' || s === 'done' || s === 'failed' ? s : 'pending'
}

function toClip(dto: ClipStateDTO): ClipState {
  return {
    id: String(dto.id),
    image: normalizeStage(dto.image),
    video: normalizeStage(dto.video),
    tts: normalizeStage(dto.tts),
    mux: normalizeStage(dto.mux),
    progress: typeof dto.progress === 'number' ? dto.progress : 0,
    message: String(dto.message ?? '')
  }
}

function nowTime(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

function levelOf(l: unknown): LogEntry['level'] {
  return l === 'warn' || l === 'error' ? l : 'info'
}

export const useQueueStore = defineStore('queue', () => {
  const name = ref('')
  const clipsState = ref<ClipState[]>([])
  const logs = ref<LogEntry[]>([])
  const connected = ref(false)
  /** 最近一次 state 读取结论：ok / not-found（后端无该剧）/ offline（连不上后端） */
  const lastRefresh = ref<'ok' | 'not-found' | 'offline' | ''>('')
  /** 后端渲染线程是否在跑（运行指示用；尽力而为，失败保持旧值） */
  const rendering = ref(false)
  /** 成片透传：finals/final_exists/mux_detail（缺字段保持旧值，不崩） */
  const finalInfo = ref<FinalInfo>(emptyFinalInfo())

  let closeStream: (() => void) | null = null
  let pollId: number | null = null
  let backoffId: number | null = null
  let fakeId: number | null = null
  let backoffAttempt = 0
  let notFoundLogged = false

  function pushLog(level: LogEntry['level'], msg: string): void {
    logs.value.push({ time: nowTime(), level, msg })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }

  function clearLogs(): void {
    logs.value = []
  }

  function upsertClip(c: ClipState): void {
    const i = clipsState.value.findIndex((x) => x.id === c.id)
    if (i >= 0) clipsState.value[i] = c
    else clipsState.value.push(c)
  }

  function stopFake(): void {
    if (fakeId !== null) {
      window.clearInterval(fakeId)
      fakeId = null
    }
  }

  /** 任何真实后端数据到达即停掉演示进度。 */
  function applyState(dto: DramaStateDTO): void {
    if (Array.isArray(dto.clips)) {
      for (const d of dto.clips) upsertClip(toClip(d))
    }
    // 成片字段缺失不崩、不清零：仅当后端显式下发才覆盖
    if (dto.finals !== undefined) finalInfo.value.finals = dto.finals
    if (dto.final_exists !== undefined) finalInfo.value.final_exists = dto.final_exists
    if (dto.mux_detail !== undefined) finalInfo.value.mux_detail = dto.mux_detail
    if (typeof dto.updated_at === 'string') finalInfo.value.updated_at = dto.updated_at
    stopFake()
    backoffAttempt = 0
  }

  function applyEvent(ev: QueueEventDTO): void {
    if (ev.state) applyState(ev.state)
    // SSE 包络可能把成片字段放在顶层（state 外）：缺字段不崩，仅显式下发才覆盖
    {
      const raw = ev as unknown as Record<string, unknown>
      if (Array.isArray(raw['finals'])) {
        const kept: FinalEntryDTO[] = []
        for (const e of raw['finals'] as Array<Record<string, unknown>>) {
          if (e && typeof e === 'object' && typeof e['file'] === 'string' && e['file'])
            kept.push(typeof e['created_at'] === 'string' ? { file: e['file'] as string, created_at: e['created_at'] as string } : { file: e['file'] as string })
        }
        finalInfo.value.finals = kept
      }
      if (typeof raw['final_exists'] === 'boolean') finalInfo.value.final_exists = raw['final_exists'] as boolean
      // 残缺对象（如 {}）收敛为默认值，模板直接读 .length 不崩
      if (raw['mux_detail'] && typeof raw['mux_detail'] === 'object') {
        const m = raw['mux_detail'] as Record<string, unknown>
        finalInfo.value.mux_detail = {
          missing_dubs: Array.isArray(m['missing_dubs']) ? (m['missing_dubs'] as string[]).map(String) : [],
          missing_srts: Array.isArray(m['missing_srts']) ? (m['missing_srts'] as string[]).map(String) : [],
          dub_ok: typeof m['dub_ok'] === 'number' ? (m['dub_ok'] as number) : 0,
          dub_total: typeof m['dub_total'] === 'number' ? (m['dub_total'] as number) : 0,
        }
      }
      if (typeof raw['updated_at'] === 'string') finalInfo.value.updated_at = raw['updated_at'] as string
    }
    if (Array.isArray(ev.clips)) {
      for (const d of ev.clips) upsertClip(toClip(d))
      stopFake()
      backoffAttempt = 0
    }
    const clipId = ev.clip_id ?? ev.clipId
    const text = String(ev.message ?? ev.msg ?? '')
    if (clipId && ev.stage) {
      stopFake()
      backoffAttempt = 0
      const cur = clipsState.value.find((c) => c.id === clipId)
      if (cur) {
        cur[ev.stage] = normalizeStage(ev.status)
        if (text) cur.message = text
      } else {
        const fresh: ClipState = {
          id: clipId,
          image: 'pending',
          video: 'pending',
          tts: 'pending',
          mux: 'pending',
          progress: 0,
          message: text
        }
        fresh[ev.stage] = normalizeStage(ev.status)
        upsertClip(fresh)
      }
      pushLog(levelOf(ev.level), `【${clipId}】${ev.stage}: ${String(ev.status ?? '')} ${text}`.trim())
    } else if (text) {
      stopFake()
      pushLog(levelOf(ev.level), text)
    }
  }

  function clearTimer(id: number | null): null {
    if (id !== null) window.clearTimeout(id)
    return null
  }

  function openStream(): void {
    try {
      closeStream?.()
    } catch {
      /* 忽略重复关闭 */
    }
    closeStream = openQueueStream(name.value || undefined, {
      onEvent: applyEvent,
      // 关闭原生自动重连，改由本层指数退避重建（1s/2s/4s…上限30s）
      onError: () => scheduleReconnect()
    })
  }

  function scheduleReconnect(): void {
    if (!connected.value || backoffId !== null) return
    try {
      closeStream?.()
    } catch {
      /* 忽略 */
    }
    closeStream = null
    const delay = Math.min(1000 * 2 ** backoffAttempt, MAX_BACKOFF_MS) + Math.floor(Math.random() * 250)
    backoffAttempt += 1
    pushLog('warn', `SSE 断开，${(delay / 1000).toFixed(1)}s 后重连（指数退避第 ${backoffAttempt} 次）`)
    backoffId = window.setTimeout(() => {
      backoffId = null
      if (!connected.value) return
      openStream()
    }, delay)
  }

  function startPoll(): void {
    if (pollId !== null) window.clearInterval(pollId)
    pollId = window.setInterval(() => {
      void refreshState(true)
    }, POLL_MS)
  }

  async function refreshState(silent = false): Promise<'ok' | 'not-found' | 'offline'> {
    if (!name.value) return 'offline'
    try {
      const dto = await fetchDramaState(name.value)
      applyState(dto)
      lastRefresh.value = 'ok'
      notFoundLogged = false
      try {
        rendering.value = await fetchRenderStatus(name.value)
      } catch {
        /* 运行指示尽力而为 */
      }
      return 'ok'
    } catch (e) {
      // 404 = 后端活着但无该剧（常因 AI 分解后没点“保存全部”）：绝不能报“不可达”
      if (e instanceof QueueHttpError && e.status === 404) {
        lastRefresh.value = 'not-found'
        if (!silent || !notFoundLogged) {
          pushLog('warn', `后端无该剧“${name.value}”：检查剧名是否打错；AI 分解完记得先去分镜表点“保存全部”同步`)
          notFoundLogged = true
        }
        return 'not-found'
      }
      lastRefresh.value = 'offline'
      if (!silent) pushLog('warn', '连不上后端，由轮询/SSE 补齐（后端恢复后自动接上）')
      return 'offline'
    }
  }

  function seedDemoClips(): void {
    const ids = ['s01', 's02', 's03', 's04']
    for (const id of ids) {
      if (!clipsState.value.some((c) => c.id === id)) {
        upsertClip({ id, image: 'pending', video: 'pending', tts: 'pending', mux: 'pending', progress: 0, message: '' })
      }
    }
    pushLog('info', '已生成本地演示分镜 s01-s04（后端恢复后自动替换为真实 state）')
  }

  function advanceFake(): void {
    const clip = clipsState.value.find((c) => STAGE_KEYS.some((k) => c[k] === 'pending' || c[k] === 'doing'))
    if (!clip) {
      stopFake()
      pushLog('info', '本地演示进度完成（全部 done）')
      return
    }
    for (const k of STAGE_KEYS) {
      if (clip[k] === 'pending') {
        clip[k] = 'doing'
        pushLog('info', `【${clip.id}】${k}: doing（本地演示）`)
        return
      }
      if (clip[k] === 'doing') {
        clip[k] = 'done'
        pushLog('info', `【${clip.id}】${k}: done（本地演示）`)
        return
      }
    }
  }

  function stopAll(): void {
    try {
      closeStream?.()
    } catch {
      /* 忽略 */
    }
    closeStream = null
    if (pollId !== null) window.clearInterval(pollId)
    pollId = null
    backoffId = clearTimer(backoffId)
    stopFake()
    backoffAttempt = 0
  }

  async function connect(target: string): Promise<void> {
    const n = target.trim()
    if (!n) {
      pushLog('warn', '请先输入剧名')
      return
    }
    stopAll()
    name.value = n
    connected.value = true
    notFoundLogged = false
    finalInfo.value = emptyFinalInfo()
    pushLog('info', `连接渲染队列：${n}（SSE 主推 + 30s 轮询兜底）`)
    const st = await refreshState()
    if (st === 'offline') {
      pushLog('warn', '后端不可达，进入本地演示模式（setInterval 假进度）')
      if (!clipsState.value.length) seedDemoClips()
      stopFake()
      fakeId = window.setInterval(advanceFake, FAKE_MS)
    } else if (st === 'not-found') {
      // 后端在线但无该剧：不跑假进度，留空等用户同步后由轮询自动接上
      clipsState.value = []
    }
    openStream()
    startPoll()
  }

  function disconnect(): void {
    stopAll()
    connected.value = false
    finalInfo.value = emptyFinalInfo()
    pushLog('info', '已断开渲染队列')
  }

  /** 断点续跑：以 state.json + 产物存在性为准重连，未完成分镜可单独重试。 */
  function resume(hint = ''): void {
    const n = hint.trim() || name.value
    if (!n) {
      pushLog('warn', '无剧名，无法断点续跑')
      return
    }
    pushLog('info', `断点续跑：${n}（成功镜不再重跑，失败镜可单独重试）`)
    void connect(n)
  }

  /**
   * 单镜重试：pointerdown 即本地置 doing 给出即时反馈，再 POST；
   * 后端复位状态后会自动开跑该镜（render_started），无后端时保留本地演示态。
   */
  async function retryClip(id: string): Promise<void> {
    const clip = clipsState.value.find((c) => c.id === id)
    if (clip) {
      let touched = false
      for (const k of STAGE_KEYS) {
        if (clip[k] === 'failed') {
          clip[k] = 'doing'
          touched = true
        }
      }
      if (!touched && clip.image !== 'done') clip.image = 'doing'
      clip.message = ''
    }
    pushLog('info', `触发单镜重试：${id}`)
    if (!name.value) {
      pushLog('warn', '未连接后端，仅本地标记为 doing 演示')
      return
    }
    try {
      const res = (await retryClipRequest(name.value, id)) as {
        render_started?: boolean
        render_note?: string
      }
      if (res.render_started) {
        rendering.value = true
        pushLog('info', `后端已开跑：${id}（${res.render_note ?? ''}）`)
      } else {
        pushLog('info', `后端已受理重跑：${id}（${res.render_note ?? '未开跑'}）`)
      }
      await refreshState(true)
    } catch {
      pushLog('error', `重跑请求失败：${id}（后端不可达，保持本地 doing 演示态）`)
    }
  }

  /** 开始渲染（全剧）：后端起 worker 真跑，重复点击 409 即“已在跑”。 */
  async function startRender(): Promise<void> {
    if (!name.value.trim()) {
      pushLog('warn', '请先输入剧名')
      return
    }
    pushLog('info', `开始渲染：${name.value.trim()}（图→视频→配音→合成）`)
    try {
      const res = await startRenderRequest(name.value)
      rendering.value = true
      pushLog('info', res.note ?? '已开跑')
      await refreshState(true)
    } catch (e) {
      pushLog('error', e instanceof Error ? e.message : String(e))
    }
  }

  return { name, clipsState, logs, connected, lastRefresh, rendering, finalInfo, connect, disconnect, resume, retryClip, startRender, refreshState, clearLogs }
})
