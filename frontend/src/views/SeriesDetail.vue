<!--
  Builder M2 · 系列详情：集列表（集名/字数/规划时长/风格/状态）+ 加一集（md/txt 上传或粘贴）
  + 每集风格覆盖框 + AI 规划预览表（hook/悬念/风险）。风格到集一级，不做逐镜风格框。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSeriesStore } from '../stores/series'
import { planEpisode, breakdownEpisode, extractEpisodeCharacters, extractNovelCharacters, NOVEL_CHUNK_CHARS, isSeriesOfflineError, episodeDramaKey } from '../api/series'
import type { PlanPreview } from '../api/series'
import { fetchModels, pickTextConfig } from '../api/drama'

const route = useRoute()
const router = useRouter()
const series = useSeriesStore()

const seriesId = computed(() => String(route.params.id ?? ''))
const notice = ref('')
const adding = ref(false)
const newTitle = ref('')
const newText = ref('')
const newStyle = ref('')
const newTotal = ref<string>('')
const fileInput = ref<HTMLInputElement | null>(null)
const planningId = ref<string | null>(null)
const plans = ref<Record<string, PlanPreview>>({})
const breakingId = ref<string | null>(null)
const extractingId = ref<string | null>(null)
const clipSeconds = ref<string>('')
const novelText = ref('')
const novelFileInput = ref<HTMLInputElement | null>(null)
const extractingNovel = ref(false)
const novelCancel = ref(false)

const novelChunkCount = computed(() => novelText.value.length ? Math.ceil(novelText.value.length / NOVEL_CHUNK_CHARS) : 0)

const textMissing = ref(false)

onMounted(async () => {
  await checkText()
  if (seriesId.value) {
    try {
      await series.selectSeries(seriesId.value)
    } catch (e) {
      notice.value = e instanceof Error ? e.message : String(e)
    }
  }
})

async function checkText(): Promise<void> {
  try {
    const data = await fetchModels()
    const t = pickTextConfig(data)
    textMissing.value = !!t && (!t.provider || !t.model)
    if (textMissing.value) notice.value = '文本模型未配置：AI 规划/分镜将被内联拒绝，请先去 /keys 设置。本次不会发出任何请求。'
  } catch {
    textMissing.value = false
  }
}

function guardText(): boolean {
  if (textMissing.value) {
    notice.value = '文本模型未配置：请先去 /keys 设置页填写提供商 / 模型，本次未发请求。'
    return false
  }
  return true
}

function parseTotalInput(): number | undefined | null {
  const raw = newTotal.value.trim()
  if (!raw) return undefined
  const n = Math.floor(Number(raw))
  if (!Number.isFinite(n) || n <= 0) {
    notice.value = `单集总时长须 > 0s，当前 ${newTotal.value}s 不合法（留空则由 AI 定）。`
    return null
  }
  return n
}

function parseClipInput(): string | undefined | null {
  const raw = clipSeconds.value.trim()
  if (!raw) return undefined
  const per = Number(raw)
  if (!Number.isFinite(per) || per < 4 || per > 12) {
    notice.value = '单镜时长需在 4-12s 之间（留空则由 AI 规划单镜；仅填写时校验）。'
    return null
  }
  return String(Math.floor(per))
}

function syncPlanTotal(epId: string, plan: PlanPreview): void {
  const t = typeof plan.total_seconds === 'number' ? plan.total_seconds
    : typeof plan.estimated_seconds === 'number' ? plan.estimated_seconds : undefined
  if (typeof t === 'number' && Number.isFinite(t)) series.applyEpisodeTotal(epId, t)
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** 加集成功后自动顺序跑全链：extract → plan → breakdown（识别先行，为规划提供角色库真名锚点）。1/3 失败即停；2/3 规划失败即停；3/3 分镜失败汇总 notice。 */
async function runFullChain(epId: string, clipOpt: string | undefined): Promise<void> {
  // 起步即清掉本集旧规划预览，避免旧结果在 AI 答复前看起来像已完成
  if (plans.value[epId]) {
    const cleared = { ...plans.value }
    delete cleared[epId]
    plans.value = cleared
  }
  // 1/3 识别角色（失败即停；成功刷新系列，规划即可用真名锚点）
  extractingId.value = epId
  notice.value = `AI 全规划 1/3 识别角色进行中…`
  let extractCount = 0
  try {
    const res = await extractEpisodeCharacters(seriesId.value, epId)
    const rawCount = typeof res?.count === 'number' ? res.count
      : Array.isArray(res?.characters) ? (res.characters as unknown[]).length : 0
    const count = Number.isFinite(rawCount) ? rawCount : 0
    const rawSkipped = typeof res?.skipped === 'number' ? res.skipped : 0
    const skipped = Number.isFinite(rawSkipped) ? rawSkipped : 0
    const note = typeof res?.note === 'string' && res.note.trim() ? res.note.trim() : ''
    if (res && res.ok === false) {
      const detail = note || '后端返回 ok=false（未给出详情）'
      series.setLastExtract({ ok: false, count, skipped, error: detail, seriesId: seriesId.value })
      notice.value = `AI 全规划 1/3 识别角色失败：${detail}；已加集保留，可点“识别角色”单独重试（规划/分镜待识别完成后跑）。`
      return
    }
    try {
      await series.selectSeries(seriesId.value)
    } catch {
      /* 角色列表刷新失败不阻断 */
    }
    series.setLastExtract({ ok: true, count, skipped, error: '', seriesId: seriesId.value })
    extractCount = series.characters.length > 0 ? series.characters.length : count
    notice.value = `AI 全规划 1/3 识别角色完成（库共 ${extractCount} 个，跳过 ${skipped} 条）→ 2/3 规划进行中…`
  } catch (e) {
    const detail = errText(e)
    series.setLastExtract({ ok: false, count: 0, skipped: 0, error: detail, seriesId: seriesId.value })
    if (isSeriesOfflineError(e)) notice.value = '后端不可达：AI 识别需后端在线+文本模型就绪，已取消本次请求；已加集保留，联网后可逐个重试（识别→规划→分镜）。'
    else notice.value = `AI 全规划 1/3 识别角色失败：${detail}；已加集保留，可点“识别角色”单独重试。`
    return
  } finally {
    extractingId.value = null
  }
  // 2/3 规划（失败即停；成功回填集时长）
  planningId.value = epId
  notice.value = `AI 全规划 2/3 规划进行中…`
  try {
    const plan = await planEpisode(seriesId.value, epId)
    plans.value = { ...plans.value, [epId]: plan }
    syncPlanTotal(epId, plan)
    const t = typeof plan.total_seconds === 'number' ? plan.total_seconds
      : typeof plan.estimated_seconds === 'number' ? plan.estimated_seconds : undefined
    const src = fmtSource(plan.total_source)
    notice.value = `AI 全规划 2/3 规划完成${typeof t === 'number' ? `（${t}s${src ? `，${src}` : ''}）` : ''} → 3/3 分镜进行中…`
  } catch (e) {
    if (isSeriesOfflineError(e)) notice.value = '后端不可达：AI 规划需后端在线+文本模型就绪，已取消本次请求；识别已保留，可点“AI 规划预览”单独重试。'
    else notice.value = `AI 全规划 2/3 规划失败：${errText(e)}；识别已保留，可点“AI 规划预览”单独重试。`
    return
  } finally {
    planningId.value = null
  }
  // 3/3 分镜（出场名已与角色库对齐，未命中保留原文写法并在 note 提示）
  breakingId.value = epId
  try {
    const ep = series.episodes.find((x) => x.id === epId)
    const style = (ep?.style_override ?? '').trim() || undefined
    const bdRes = await breakdownEpisode(seriesId.value, epId, {
      ...(clipOpt !== undefined ? { clip_seconds: clipOpt } : {}),
      ...(style ? { style } : {})
    })
    const bdMiss = Array.isArray(bdRes?.missing_assets) ? bdRes.missing_assets.length : 0
    const bdNote = typeof bdRes?.note === 'string' && bdRes.note.trim() ? `；${bdRes.note.trim()}` : ''
    notice.value = `AI 全规划 3/3 完成：识别库共 ${extractCount} 个角色，分镜已生成${bdMiss > 0 ? `（待补图 ${bdMiss} 处，模式已保留）` : ''}${bdNote}，去分镜表/角色页确认。`
  } catch (e) {
    notice.value = `AI 全规划 3/3 分镜失败：${errText(e)}；识别与规划已保留，可点“AI 分镜初稿”单独重试，去分镜表/角色页确认。`
  } finally {
    breakingId.value = null
  }
}

/** F2-1 每集独立识别：调 extractEpisodeCharacters，显示 count/skipped，成功刷新系列并 setLastExtract；复用 extractingId 单集互斥。 */
async function onExtract(epId: string): Promise<void> {
  if (!guardText()) return
  if (planningId.value || breakingId.value || extractingId.value || adding.value) {
    notice.value = '已有规划/分镜/识别/加集任务进行中，请稍后再试（单集互斥）。'
    return
  }
  extractingId.value = epId
  notice.value = '识别角色进行中…'
  try {
    const res = await extractEpisodeCharacters(seriesId.value, epId)
    const rawCount = typeof res?.count === 'number' ? res.count
      : Array.isArray(res?.characters) ? (res.characters as unknown[]).length : 0
    const count = Number.isFinite(rawCount) ? rawCount : 0
    const rawSkipped = typeof res?.skipped === 'number' ? res.skipped : 0
    const skipped = Number.isFinite(rawSkipped) ? rawSkipped : 0
    const note = typeof res?.note === 'string' && res.note.trim() ? res.note.trim() : ''
    if (res && res.ok === false) {
      const detail = note || '后端返回 ok=false（未给出详情）'
      series.setLastExtract({ ok: false, count, skipped, error: detail, seriesId: seriesId.value })
      notice.value = `识别失败：${detail}；可重试或去角色页手动补。`
      return
    }
    try {
      await series.selectSeries(seriesId.value)
    } catch {
      /* 刷新失败不阻断 */
    }
    series.setLastExtract({ ok: true, count, skipped, error: '', seriesId: seriesId.value })
    notice.value = `识别完成：${count} 个角色（跳过 ${skipped} 条）${note ? `；${note}` : ''}，去角色页确认。`
  } catch (e) {
    const detail = errText(e)
    series.setLastExtract({ ok: false, count: 0, skipped: 0, error: detail, seriesId: seriesId.value })
    if (isSeriesOfflineError(e)) notice.value = `后端不可达：识别已取消（${detail}）；联网后重试。`
    else notice.value = `识别失败：${detail}；可重试或去角色页手动补。`
  } finally {
    extractingId.value = null
  }
}

/** 从文件导入整本小说：本地 File.text() 读入 novelText，不建集不调后端。 */
async function onImportNovelFile(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  try {
    const text = await f.text()
    novelText.value = text
    if (!text.trim()) {
      notice.value = '导入文件为空：请选择包含正文的 .md/.txt 文件。'
      return
    }
    const n = Math.ceil(text.length / NOVEL_CHUNK_CHARS)
    notice.value = `已导入全文 ${text.length} 字 → ${n} 段（每段≤12000字），可点“开始全文识别”。`
  } catch (err) {
    notice.value = err instanceof Error ? err.message : String(err)
  }
}

/** 全文识别：按 NOVEL_CHUNK_CHARS 硬切串行 extractNovelCharacters，失败记首错继续；成功后加集默认跳过自动识别。 */
async function onExtractNovel(): Promise<void> {
  if (!guardText()) return
  if (!novelText.value.trim()) {
    notice.value = '请先粘贴全文或从文件导入后再开始全文识别。'
    return
  }
  if (adding.value || planningId.value !== null || breakingId.value !== null || extractingId.value !== null || extractingNovel.value) {
    notice.value = '已有规划/分镜/识别/加集/全文识别任务进行中，请稍后再试（单集互斥）。'
    return
  }
  const text = novelText.value
  const chunks: string[] = []
  for (let i = 0; i < text.length; i += NOVEL_CHUNK_CHARS) {
    chunks.push(text.slice(i, i + NOVEL_CHUNK_CHARS))
  }
  const total = chunks.length
  if (!total) {
    notice.value = '请先粘贴全文或从文件导入后再开始全文识别。'
    return
  }
  extractingNovel.value = true
  novelCancel.value = false
  let okCount = 0
  let failCount = 0
  let skippedTotal = 0
  let firstError = ''
  let lastCount = 0
  try {
    for (let i = 0; i < chunks.length; i++) {
      if (novelCancel.value) break
      notice.value = `全文识别 第${i + 1}/${total}段…`
      const chunk = chunks[i] as string
      try {
        const res = await extractNovelCharacters(seriesId.value, chunk)
        const rawCount = typeof res?.count === 'number' ? res.count
          : Array.isArray(res?.characters) ? (res.characters as unknown[]).length : 0
        const count = Number.isFinite(rawCount) ? rawCount : 0
        const rawSkipped = typeof res?.skipped === 'number' ? res.skipped : 0
        const skipped = Number.isFinite(rawSkipped) ? rawSkipped : 0
        const note = typeof res?.note === 'string' && res.note.trim() ? res.note.trim() : ''
        if (res && res.ok === false) {
          failCount++
          if (!firstError) firstError = note || '后端返回 ok=false（未给出详情）'
          continue
        }
        lastCount = count
        skippedTotal += skipped
        okCount++
      } catch (e) {
        failCount++
        if (!firstError) firstError = errText(e)
      }
    }
    try {
      await series.selectSeries(seriesId.value)
    } catch {
      /* 刷新失败不阻断汇总 */
    }
    const finalCount = series.characters.length > 0 ? series.characters.length : lastCount
    const overallOk = failCount === 0 && !novelCancel.value
    series.setLastExtract({
      ok: overallOk,
      count: finalCount,
      skipped: skippedTotal,
      error: overallOk ? '' : (firstError || (novelCancel.value ? '已取消' : '')),
      seriesId: seriesId.value
    })
    if (novelCancel.value) {
      notice.value = `已取消全文识别：成功 ${okCount} 段、失败 ${failCount} 段，共识别 ${finalCount} 个（跳过 ${skippedTotal} 条），已刷新成功部分，去角色页确认。`
    } else if (overallOk) {
      notice.value = `全文识别完成：${total} 段共识别 ${finalCount} 个角色（跳过 ${skippedTotal} 条），之后加集默认不再自动识别；去角色页确认。`
    } else if (okCount > 0) {
      notice.value = `全文识别部分完成：成功 ${okCount} 段、失败 ${failCount} 段（首错：${firstError}），共识别 ${finalCount} 个（跳过 ${skippedTotal} 条），已刷新成功部分，去角色页确认。`
    } else {
      notice.value = `全文识别失败：${total} 段均失败（首错：${firstError}）。`
    }
  } finally {
    extractingNovel.value = false
    novelCancel.value = false
  }
}

async function onAdd(): Promise<void> {
  notice.value = ''
  if (!newTitle.value.trim() && !newText.value.trim()) {
    notice.value = '请填写集名或粘贴正文后再加一集。'
    return
  }
  const total = parseTotalInput()
  if (total === null) return
  const clipOpt = parseClipInput()
  if (clipOpt === null) return
  const shortWarn = total !== undefined && total < 4 ? '（<4s 暂无法生成视频，仅搭架子）' : ''
  adding.value = true
  try {
    const ep = await series.addEpisode({
      title: newTitle.value.trim() || `第${series.episodes.length + 1}集`,
      source_text: newText.value,
      style_override: newStyle.value.trim(),
      ...(total !== undefined ? { total_seconds: total } : {})
    })
    if (series.lastSource === 'local') {
      notice.value = `已存本地集“${ep.title}”（后端不可达，联网后由后端重建；联网后再跑全链）${shortWarn}`
      newTitle.value = ''
      newText.value = ''
      return
    }
    const addedTitle = ep.title
    newTitle.value = ''
    newText.value = ''
    if (!guardText()) {
      notice.value = `已加一集“${addedTitle}”${shortWarn}；${notice.value}（可点“AI 规划预览”单独重试）`
      return
    }
    notice.value = `已加一集“${addedTitle}”${shortWarn}；AI 全规划 1/3 识别角色 → 2/3 规划 → 3/3 分镜进行中…`
    await runFullChain(ep.id, clipOpt)
  } catch (e) {
    notice.value = errText(e)
  } finally {
    adding.value = false
  }
}

async function onPickFile(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  notice.value = ''
  const total = parseTotalInput()
  if (total === null) return
  const clipOpt = parseClipInput()
  if (clipOpt === null) return
  adding.value = true
  try {
    const ep = await series.addEpisodeFile(f, {
      title: newTitle.value.trim() || undefined,
      style_override: newStyle.value.trim() || undefined,
      ...(total !== undefined ? { total_seconds: total } : {})
    })
    if (series.lastSource === 'local') {
      notice.value = `已存本地集“${ep.title}”（后端不可达，联网后由后端重建；联网后再跑全链）`
      return
    }
    if (!guardText()) {
      notice.value = `已从文件加一集“${ep.title}”（${ep.word_count ?? 0}字）；${notice.value}（可单独重试）`
      return
    }
    notice.value = `已从文件加一集“${ep.title}”（${ep.word_count ?? 0}字）；AI 全规划 1/3 识别角色 → 2/3 规划 → 3/3 分镜进行中…`
    await runFullChain(ep.id, clipOpt)
  } catch (err) {
    notice.value = errText(err)
  } finally {
    adding.value = false
  }
}

async function onPlan(epId: string): Promise<void> {
  if (!guardText()) return
  if (planningId.value || breakingId.value || extractingId.value || adding.value) {
    notice.value = '已有规划/分镜/识别/加集任务进行中，请稍后再试（单集互斥）。'
    return
  }
  planningId.value = epId
  notice.value = ''
  // 起步即清掉本集旧规划预览，避免旧结果在 AI 答复前看起来像已完成
  if (plans.value[epId]) {
    const cleared = { ...plans.value }
    delete cleared[epId]
    plans.value = cleared
  }
  try {
    const plan = await planEpisode(seriesId.value, epId)
    plans.value = { ...plans.value, [epId]: plan }
    syncPlanTotal(epId, plan)
    const t = typeof plan.total_seconds === 'number' ? plan.total_seconds
      : typeof plan.estimated_seconds === 'number' ? plan.estimated_seconds : undefined
    const src = fmtSource(plan.total_source)
    notice.value = typeof t === 'number'
      ? `规划完成：${t}s${src ? `（${src}）` : ''}，已回填集时长。`
      : '规划完成，已回填。'
  } catch (e) {
    if (isSeriesOfflineError(e)) notice.value = '后端不可达：AI 规划需后端在线+文本模型就绪，已取消本次请求。'
    else notice.value = errText(e)
  } finally {
    planningId.value = null
  }
}

async function onBreakdown(epId: string): Promise<void> {
  if (!guardText()) return
  if (planningId.value || breakingId.value || extractingId.value || adding.value) {
    notice.value = '已有规划/分镜/识别/加集任务进行中，请稍后再试（单集互斥）。'
    return
  }
  const clipOpt = parseClipInput()
  if (clipOpt === null) return
  breakingId.value = epId
  notice.value = ''
  const ep = series.episodes.find((x) => x.id === epId)
  try {
    const style = (ep?.style_override ?? '').trim() || undefined
    const res = await breakdownEpisode(seriesId.value, epId, {
      ...(clipOpt !== undefined ? { clip_seconds: clipOpt } : {}),
      ...(style ? { style } : {})
    })
    const note = typeof res?.note === 'string' && res.note.trim() ? `（${res.note.trim()}）` : ''
    const miss = Array.isArray(res?.missing_assets) && res.missing_assets.length > 0
      ? `待补图 ${res.missing_assets.length} 处（模式已保留，去分镜表/角色页补传或 AI 生成）` : ''
    notice.value = `第 ${epId} 集 AI 分镜初稿已生成并同步（分镜表/队列可用集 key 直接打开）${note}${miss ? `；${miss}` : ''}：请检查修改后保存。`
  } catch (e) {
    notice.value = errText(e)
  } finally {
    breakingId.value = null
  }
}

function goQueue(epId: string): void {
  const ep = series.episodes.find((x) => x.id === epId)
  const key = ep ? episodeDramaKey(ep) : epId
  void router.push({ path: '/queue', query: { series: seriesId.value, ep: epId, drama: key } })
}

function goShots(epId: string): void {
  const ep = series.episodes.find((x) => x.id === epId)
  const key = ep ? episodeDramaKey(ep) : epId
  void router.push({ path: '/shots', query: { name: key } })
}

function fmtPlan(p: PlanPreview): { hook: string; cliff: string; risks: string[] } {
  return {
    hook: typeof p.hook === 'string' ? p.hook : '—',
    cliff: typeof p.cliffhanger === 'string' ? p.cliffhanger : '—',
    risks: Array.isArray(p.risks) ? p.risks.map(String) : []
  }
}

function fmtClips(v: unknown): string {
  if (!Array.isArray(v) || !v.length) return '—'
  const nums = (v as unknown[]).filter((n): n is number => typeof n === 'number')
  if (!nums.length) return '—'
  return `${nums.join('+')}s`
}

function fmtDensity(v: PlanPreview['density']): string {
  if (!v || typeof v !== 'object') return '—'
  const cps = typeof v.chars_per_sec === 'number' ? v.chars_per_sec : undefined
  const dm = typeof v.dialogue_max === 'number' ? v.dialogue_max : undefined
  if (cps == null && dm == null) return '—'
  return `${cps ?? '—'} 字/s · 对白上限 ${dm ?? '—'} 字`
}

function fmtSource(v: unknown): string {
  if (typeof v !== 'string') return ''
  const s = v.trim().toLowerCase()
  if (s === 'ai') return 'AI 规划'
  if (s === 'manual') return '手动填写'
  return ''
}
</script>

<template>
  <div class="page">
    <header class="toolbar"><div class="toolbar-inner">
      <span class="brand">系列 · {{ series.currentSeries?.title ?? seriesId }}</span>
      <span class="actions">
        <router-link class="link" :to="`/series/${encodeURIComponent(seriesId)}/characters`">角色</router-link>
        <router-link class="link" to="/series/new">新建系列</router-link>
      </span>
    </div></header>
    <main class="wrap">
      <div v-if="textMissing" class="notice error">文本模型未配置：AI 规划/分镜将被内联拒绝，请先去 <router-link class="link" to="/keys">/keys 设置</router-link>。本次不会发出任何请求。</div>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <p v-if="series.lastSource === 'local'" class="notice warn">后端不可达，当前为本地索引：加集/改风格先落本地。</p>

      <section class="panel">
        <h2>加一集（.md/.txt 上传或粘贴）</h2>
        <div class="grid2">
          <label class="field"><span>集名</span><input v-model="newTitle" type="text" placeholder="如：第1集 雪夜入门" maxlength="60" /></label>
          <label class="field"><span>本集风格覆盖（留空=沿用系列默认；风格只到集一级）</span><input v-model="newStyle" type="text" placeholder="如：国风水墨" maxlength="40" /></label>
        </div>
        <label class="field"><span>正文粘贴</span><textarea v-model="newText" rows="6" placeholder="把本集小说/剧本正文粘贴到这里" maxlength="50000"></textarea></label>
        <div class="row">
          <label class="field"><span>规划时长（秒，留空由 AI 定；填写须 &gt; 0，&lt;4s 暂无法生成视频）</span><input v-model="newTotal" type="text" inputmode="numeric" placeholder="留空由 AI 定" /></label>
          <label class="field"><span>单镜秒（留空由 AI 规划单镜；填写须 4-12）</span><input v-model="clipSeconds" type="text" inputmode="numeric" placeholder="留空由 AI 规划单镜" /></label>
        </div>
        <p class="hint">留空由 AI 定：AI 按原文量/密度提议总时长与单镜切分；单镜 4-12s 按剧情节奏混剪，末镜收尾对齐总时长。</p>
        <div class="btn-row">
          <button type="button" class="primary" :disabled="adding || extractingNovel || planningId !== null || breakingId !== null || extractingId !== null" @click="onAdd">{{ adding ? '加入中…' : '加一集（粘贴）' }}</button>
          <button type="button" class="ghost" :disabled="adding || extractingNovel || planningId !== null || breakingId !== null || extractingId !== null" @click="fileInput?.click()">上传 .md/.txt</button>
          <input ref="fileInput" type="file" accept=".md,.txt,text/markdown,text/plain" hidden @change="onPickFile" />
        </div>
      </section>

      <section class="panel">
        <h2>从整本小说识别角色（一次即可）</h2>
        <label class="field"><span>全文粘贴</span><textarea v-model="novelText" rows="6" placeholder="把整本小说正文粘贴到这里（一次识别即可）"></textarea></label>
        <p class="hint">全文 {{ novelText.length }} 字 → {{ novelChunkCount }} 段（每段≤12000字）</p>
        <div class="btn-row">
          <button type="button" class="ghost" :disabled="adding || planningId !== null || breakingId !== null || extractingId !== null || extractingNovel" @click="novelFileInput?.click()">从文件导入.md/.txt</button>
          <input ref="novelFileInput" type="file" accept=".md,.txt" hidden @change="onImportNovelFile" />
          <button type="button" class="primary" :disabled="!novelText.trim() || adding || planningId !== null || breakingId !== null || extractingId !== null || extractingNovel" @click="onExtractNovel">{{ extractingNovel ? '全文识别中…' : '开始全文识别' }}</button>
          <button type="button" class="ghost" :disabled="!extractingNovel" @click="novelCancel = true">取消</button>
        </div>
        <p class="hint">一次识别后写入角色库，之后加集默认不再自动识别；新角色可在角色页补识别。</p>
      </section>

      <section class="panel">
        <h2>集列表（{{ series.episodes.length }}）</h2>
        <p v-if="!series.episodes.length" class="hint">暂无集：先在上方加一集。</p>
        <ul class="eps">
          <li v-for="ep in series.episodes" :key="ep.id" class="ep">
            <div class="ep-head">
              <strong>{{ ep.title }}</strong>
              <span class="meta">{{ ep.word_count != null ? `${ep.word_count}字 · ` : '' }}{{ (ep.planned_seconds ?? ep.total_seconds) != null ? `规划 ${(ep.planned_seconds ?? ep.total_seconds)}s · ` : '' }}{{ ep.style_override || '沿用系列风格' }} · {{ ep.status ?? '—' }}</span>
            </div>
            <div class="btn-row">
              <button type="button" class="ghost sm" :disabled="planningId !== null || breakingId !== null || extractingId !== null || adding || extractingNovel" @click="onPlan(ep.id)">{{ planningId === ep.id ? '规划中…' : 'AI 规划预览' }}</button>
              <button type="button" class="ghost sm" :disabled="planningId !== null || breakingId !== null || extractingId !== null || adding || extractingNovel" @click="onBreakdown(ep.id)">{{ breakingId === ep.id ? '分镜中…' : extractingId === ep.id ? '识别角色中…' : 'AI 分镜初稿' }}</button>
              <button type="button" class="ghost sm" :disabled="planningId !== null || breakingId !== null || extractingId !== null || adding || extractingNovel" @click="onExtract(ep.id)">{{ extractingId === ep.id ? '识别中…' : '识别角色' }}</button>
              <button type="button" class="ghost sm" @click="goShots(ep.id)">去分镜表</button>
              <button type="button" class="ghost sm" @click="goQueue(ep.id)">去队列渲染</button>
            </div>
            <div v-if="plans[ep.id]" class="plan" role="status">
              <p><strong>hook：</strong>{{ fmtPlan(plans[ep.id] as PlanPreview).hook }}</p>
              <p><strong>悬念：</strong>{{ fmtPlan(plans[ep.id] as PlanPreview).cliff }}</p>
              <p><strong>风险：</strong>{{ fmtPlan(plans[ep.id] as PlanPreview).risks.length ? fmtPlan(plans[ep.id] as PlanPreview).risks.join('；') : '—' }}</p>
              <p><strong>单镜表：</strong>{{ fmtClips((plans[ep.id] as PlanPreview).clip_durations) }}</p>
              <p><strong>密度：</strong>{{ fmtDensity((plans[ep.id] as PlanPreview).density) }}</p>
              <p v-if="fmtSource((plans[ep.id] as PlanPreview).total_source)"><strong>时长来源：</strong>{{ fmtSource((plans[ep.id] as PlanPreview).total_source) }}</p>
              <p><strong>beats：</strong>{{ (plans[ep.id] as PlanPreview).beats?.length ? '' : '—' }}</p>
              <ul v-if="(plans[ep.id] as PlanPreview).beats?.length">
                <li v-for="(b, i) in (plans[ep.id] as PlanPreview).beats" :key="i">{{ b }}</li>
              </ul>
              <p v-if="(plans[ep.id] as PlanPreview).estimated_seconds != null" class="hint">预估时长：{{ (plans[ep.id] as PlanPreview).estimated_seconds }}s</p>
            </div>
          </li>
        </ul>
      </section>
    </main>
  </div>
</template>

<style scoped>
.page { min-height: 100vh; background: #f5f5f7; color: #1d1d1f; }
.toolbar { position: sticky; top: 0; z-index: 10; background: rgba(255,255,255,.65); backdrop-filter: blur(20px) saturate(180%); border-bottom: 1px solid rgba(0,0,0,.08); }
.toolbar-inner { max-width: 860px; margin: 0 auto; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.brand { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.actions { display: flex; gap: 12px; }
.wrap { max-width: 860px; margin: 0 auto; padding: 20px 16px 64px; }
.panel { background: rgba(255,255,255,.8); border: 1px solid rgba(0,0,0,.06); border-radius: 16px; padding: 16px; margin: 14px 0; }
.field { display: flex; flex-direction: column; gap: 6px; margin: 10px 0; font-size: 14px; font-weight: 600; }
input, textarea { font: inherit; font-weight: 400; padding: 9px 11px; border-radius: 10px; border: 1px solid rgba(0,0,0,.15); background: #fff; }
.hint { color: #6e6e73; font-size: 13px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.btn-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
.primary { font: inherit; font-weight: 700; padding: 10px 18px; border: none; border-radius: 12px; background: #0a84ff; color: #fff; cursor: pointer; }
.ghost { font: inherit; padding: 9px 14px; border-radius: 12px; border: 1px solid rgba(0,0,0,.15); background: transparent; cursor: pointer; }
.ghost.sm { padding: 6px 12px; font-size: 13px; }
button:disabled { opacity: .5; cursor: not-allowed; }
.notice { padding: 10px 12px; border-radius: 10px; background: rgba(0,0,0,.05); margin: 12px 0; }
.notice.error { background: rgba(255,59,48,.14); } .notice.warn { background: rgba(255,159,10,.18); }
.link { color: #0a84ff; }
.eps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.ep { border: 1px solid rgba(0,0,0,.08); border-radius: 12px; padding: 10px 12px; background: rgba(255,255,255,.6); }
.ep-head { display: flex; flex-direction: column; gap: 4px; }
.meta { font-size: 12px; color: #6e6e73; }
.plan { margin-top: 8px; border-left: 3px solid #0a84ff; padding-left: 10px; font-size: 14px; }
.plan p { margin: 4px 0; }
@media (max-width: 560px) { .grid2, .row { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) { .page { background: #000; color: #f5f5f7; } .toolbar { background: rgba(20,20,22,.6); } .panel, .ep { background: rgba(28,28,30,.8); } input, textarea { background: #1c1c1e; color: #f5f5f7; } }
</style>
