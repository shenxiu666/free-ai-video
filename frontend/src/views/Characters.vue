<!-- Builder M2 · 角色页：角色卡（名/简介/立绘/设主图/删/合并）+“生成立绘”二次确认与节流。前端只见立绘 URL/掩码。 -->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useSeriesStore } from '../stores/series'
import { updateCharacter, deleteCharacter, mergeCharacters, generatePortrait, uploadPortrait, extractEpisodeCharacters, isSeriesOfflineError, resolvePortraitUrl, resolvePortraitPath } from '../api/series'
import type { SeriesCharacter } from '../api/series'

const route = useRoute()
const series = useSeriesStore()
const seriesId = computed(() => String(route.params.id ?? ''))

const notice = ref('')
const newName = ref('')
const newDesc = ref('')
const mergeTarget = ref('')
const mergeSources = ref<string[]>([])
const confirmPortraitId = ref<string | null>(null)
const portraitBusyId = ref<string | null>(null)
const confirmingId = ref<string | null>(null)
/** U2 立绘上传：共用隐藏 file input + 待传角色 id（选中即传）；busy 与 AI 生成共用 portraitBusyId 互斥。 */
const uploadInput = ref<HTMLInputElement | null>(null)
const pendingUploadCharId = ref<string | null>(null)
const PORTRAIT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
/** F2-3 一键识别本系列：逐集串行 extract 的总开关 + 当前集 id（progress notice 用）。 */
const extractingAll = ref(false)
const extractingEpId = ref<string | null>(null)
/** H 卡片编辑：当前展开卡 id + 按卡暂存的表单草稿（取消不写回）。 */
const editingId = ref<string | null>(null)
const savingId = ref<string | null>(null)
interface CharacterDraft {
  name: string
  desc: string
  aliases: string
  appearance: string
  personality: string
  relation: string
  outfit: string
  note: string
}
const drafts = ref<Record<string, CharacterDraft>>({})
/** 节流：每角色 60s 内只允许生成一次（前端节流，后端仍以队列串行兜底）。 */
const lastPortraitAt = ref<Record<string, number>>({})
const nowTick = ref(Date.now())
let tickTimer: number | null = null

const PORTRAIT_COOLDOWN_MS = 60_000

/** 仅展示当前系列的识别记录（跨系列切换时视为从未识别）。 */
const extractForSeries = computed(() => {
  const le = series.lastExtract
  if (!le) return null
  if (le.seriesId && le.seriesId !== seriesId.value) return null
  return le
})

onMounted(async () => {
  tickTimer = window.setInterval(() => { nowTick.value = Date.now() }, 1000)
  if (seriesId.value) {
    try {
      await series.selectSeries(seriesId.value)
    } catch (e) {
      notice.value = e instanceof Error ? e.message : String(e)
    }
  }
})

onUnmounted(() => {
  if (tickTimer !== null) {
    window.clearInterval(tickTimer)
    tickTimer = null
  }
})

function cooldownLeft(charId: string): number {
  const last = lastPortraitAt.value[charId] ?? 0
  const left = PORTRAIT_COOLDOWN_MS - (nowTick.value - last)
  return left > 0 ? Math.ceil(left / 1000) : 0
}

async function onAdd(): Promise<void> {
  notice.value = ''
  try {
    await series.addCharacter({ name: newName.value, desc: newDesc.value })
    newName.value = ''
    newDesc.value = ''
    notice.value = '角色已添加（离线时仅存本地）。'
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

/** “生成立绘”两段式二次确认 + 节流：第一次点进入确认态，第二次才真发请求。 */
async function onPortrait(charId: string): Promise<void> {
  notice.value = ''
  if (portraitBusyId.value !== null) {
    notice.value = '已有立绘任务进行中，请稍候再试。'
    return
  }
  if (pendingUploadCharId.value !== null) {
    notice.value = '已有上传待选择文件，请先完成或取消该上传。'
    return
  }
  const left = cooldownLeft(charId)
  if (left > 0) {
    notice.value = `节流中：该角色 ${left}s 后可再次生成（避免烧配额）。`
    return
  }
  if (confirmPortraitId.value !== charId) {
    confirmPortraitId.value = charId
    notice.value = '再点一次确认生成立绘（将调用图片模型，注意配额）。'
    return
  }
  confirmPortraitId.value = null
  portraitBusyId.value = charId
  try {
    const res = await generatePortrait(seriesId.value, charId, { style: series.currentSeries?.style ?? '' })
    lastPortraitAt.value = { ...lastPortraitAt.value, [charId]: Date.now() }
    const url = resolvePortraitUrl(res)
    if (url) {
      const c = series.characters.find((x) => x.id === charId)
      if (c) c.portrait_url = url
      notice.value = '立绘已生成。'
    } else {
      const p = resolvePortraitPath(res)
      if (p) {
        if (p.startsWith('http') || p.startsWith('file') || p.startsWith('data') || p.startsWith('/')) {
          const c = series.characters.find((x) => x.id === charId)
          if (c) c.portrait_url = p
          notice.value = '立绘已生成。'
        } else {
          notice.value = `已存 ${p}，刷新角色卡查看`
        }
      } else {
        notice.value = '后端已受理生成（未返回直链），稍后刷新查看。'
      }
    }
  } catch (e) {
    if (isSeriesOfflineError(e)) notice.value = '后端不可达：生成立绘需后端在线，已取消本次请求。'
    else notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    portraitBusyId.value = null
  }
}

/** U2 “上传立绘”：隐藏 input 选中即传；5MB 前端预检；busy 与 AI 生成共用 portraitBusyId 全局互斥（非空即忙）。 */
function triggerUpload(charId: string): void {
  if (portraitBusyId.value !== null) {
    notice.value = '已有立绘任务进行中，请稍候再试。'
    return
  }
  if (pendingUploadCharId.value !== null) {
    notice.value = '已有上传待选择文件，请先完成或取消该上传。'
    return
  }
  notice.value = ''
  pendingUploadCharId.value = charId
  uploadInput.value?.click()
}

/** 文件弹框取消（支持 cancel 事件的浏览器）：清除挂起，避免永久锁死上传按钮。 */
function onUploadCancel(): void {
  pendingUploadCharId.value = null
}

async function onUploadChange(e: Event): Promise<void> {
  const el = e.target as HTMLInputElement | null
  const charId = pendingUploadCharId.value
  const file = el?.files?.[0]
  if (!file || !charId) {
    pendingUploadCharId.value = null
    if (el) el.value = ''
    return
  }
  pendingUploadCharId.value = null
  if (file.size > PORTRAIT_UPLOAD_MAX_BYTES) {
    notice.value = '文件过大：立绘请上传 ≤5MB 的图片（PNG/JPEG/WebP），已取消本次请求。'
    el.value = ''
    return
  }
  notice.value = ''
  portraitBusyId.value = charId
  try {
    const res = await uploadPortrait(seriesId.value, charId, file)
    const url = resolvePortraitUrl(res)
    if (url) {
      const c = series.characters.find((x) => x.id === charId)
      if (c) c.portrait_url = url
      notice.value = '立绘已上传。'
    } else {
      const p = resolvePortraitPath(res)
      if (p) {
        if (p.startsWith('http') || p.startsWith('file') || p.startsWith('data') || p.startsWith('/')) {
          const c = series.characters.find((x) => x.id === charId)
          if (c) c.portrait_url = p
          notice.value = '立绘已上传。'
        } else {
          notice.value = `已存 ${p}，刷新角色卡查看`
        }
      } else {
        notice.value = '后端已受理上传（未返回直链），稍后刷新查看。'
      }
    }
  } catch (err) {
    if (isSeriesOfflineError(err)) notice.value = '后端不可达：上传立绘需后端在线，已取消本次请求。'
    else notice.value = err instanceof Error ? err.message : String(err)
  } finally {
    portraitBusyId.value = null
    if (el) el.value = ''
  }
}

async function onSetMain(charId: string): Promise<void> {
  notice.value = ''
  if (portraitBusyId.value !== null || pendingUploadCharId.value !== null) {
    notice.value = '已有立绘任务进行中，请稍候再试。'
    return
  }
  try {
    await updateCharacter(seriesId.value, charId, { is_main: true })
    for (const c of series.characters) c.is_main = c.id === charId
    notice.value = '已设为主图。'
  } catch (e) {
    if (isSeriesOfflineError(e)) {
      for (const c of series.characters) c.is_main = c.id === charId
      notice.value = '后端不可达：已在本地标记主图，联网后重设一次。'
    } else notice.value = e instanceof Error ? e.message : String(e)
  }
}

/** 状态确认：待确认 → 已确认（PATCH {status:"已确认"}，离线时本地标记）。 */
async function onConfirm(charId: string): Promise<void> {
  notice.value = ''
  confirmingId.value = charId
  try {
    await updateCharacter(seriesId.value, charId, { status: '已确认' })
    const c = series.characters.find((x) => x.id === charId)
    if (c) c.status = '已确认'
    notice.value = '角色已确认。'
  } catch (e) {
    if (isSeriesOfflineError(e)) {
      const c = series.characters.find((x) => x.id === charId)
      if (c) c.status = '已确认'
      notice.value = '后端不可达：已在本地标记已确认，联网后重设一次。'
    } else notice.value = e instanceof Error ? e.message : String(e)
  } finally {
    confirmingId.value = null
  }
}

async function onDelete(charId: string): Promise<void> {
  notice.value = ''
  if (portraitBusyId.value !== null || pendingUploadCharId.value !== null) {
    notice.value = '已有立绘任务进行中，请稍候再删（避免上传中删卡致 404/孤儿）。'
    return
  }
  try {
    await deleteCharacter(seriesId.value, charId)
    series.removeLocalCharacter(charId)
    notice.value = '角色已删除。'
  } catch (e) {
    if (isSeriesOfflineError(e)) {
      series.removeLocalCharacter(charId)
      notice.value = '后端不可达：已删除本地角色。'
    } else notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function onMerge(): Promise<void> {
  notice.value = ''
  if (!mergeTarget.value || !mergeSources.value.length) {
    notice.value = '请选择合并目标与至少一个来源。'
    return
  }
  if (mergeSources.value.includes(mergeTarget.value)) {
    notice.value = '来源不能包含目标本身。'
    return
  }
  try {
    await mergeCharacters(seriesId.value, { source_ids: [...mergeSources.value], target_id: mergeTarget.value })
    series.characters = series.characters.filter((c) => !mergeSources.value.includes(c.id))
    mergeSources.value = []
    notice.value = '已合并（来源已并入目标）。'
  } catch (e) {
    if (isSeriesOfflineError(e)) {
      series.characters = series.characters.filter((c) => !mergeSources.value.includes(c.id))
      mergeSources.value = []
      notice.value = '后端不可达：已在本地合并，联网后由后端重建时再合一次。'
    } else notice.value = e instanceof Error ? e.message : String(e)
  }
}

function toggleSource(id: string): void {
  mergeSources.value = mergeSources.value.includes(id)
    ? mergeSources.value.filter((x) => x !== id)
    : [...mergeSources.value, id]
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** F2-3 一键识别本系列：按集顺序逐集调 extract，单集失败收集继续；完成后刷新 + setLastExtract（ok 取整体，error 取首条）。 */
async function onExtractAll(): Promise<void> {
  if (extractingAll.value) return
  notice.value = ''
  try {
    if (!series.episodes.length) {
      try {
        await series.selectSeries(seriesId.value)
      } catch {
        /* 刷新失败则按空集处理 */
      }
    }
  } catch {
    /* 忽略预刷新错误 */
  }
  if (!series.episodes.length) {
    notice.value = '本系列暂无集：请先回系列页加一集。'
    return
  }
  extractingAll.value = true
  let total = 0
  let skippedTotal = 0
  let okEps = 0
  let failEps = 0
  let firstError = ''
  const eps = [...series.episodes]
  try {
    for (let i = 0; i < eps.length; i++) {
      const ep = eps[i] as { id: string; title: string }
      extractingEpId.value = ep.id
      const label = `E${String(i + 1).padStart(2, '0')}`
      notice.value = `识别 ${label}（${ep.title}）…（${i + 1}/${eps.length}）`
      try {
        const res = await extractEpisodeCharacters(seriesId.value, ep.id)
        const rawCount = typeof res?.count === 'number' ? res.count
          : Array.isArray(res?.characters) ? (res.characters as unknown[]).length : 0
        const count = Number.isFinite(rawCount) ? rawCount : 0
        const rawSkipped = typeof res?.skipped === 'number' ? res.skipped : 0
        const skipped = Number.isFinite(rawSkipped) ? rawSkipped : 0
        const note = typeof res?.note === 'string' && res.note.trim() ? res.note.trim() : ''
        if (res && res.ok === false) {
          failEps++
          if (!firstError) firstError = note || '后端返回 ok=false（未给出详情）'
          continue
        }
        total += count
        skippedTotal += skipped
        okEps++
      } catch (e) {
        failEps++
        if (!firstError) firstError = errText(e)
      }
    }
    try {
      await series.selectSeries(seriesId.value)
    } catch {
      /* 刷新失败不阻断汇总 */
    }
    const overallOk = failEps === 0
    series.setLastExtract({
      ok: overallOk,
      count: total,
      skipped: skippedTotal,
      error: overallOk ? '' : `${firstError}${failEps > 1 ? `（另有 ${failEps - 1} 集失败）` : ''}`,
      seriesId: seriesId.value
    })
    if (overallOk) {
      notice.value = `一键识别完成：${eps.length} 集共识别 ${total} 个角色（跳过 ${skippedTotal} 条），去下方角色卡确认。`
    } else if (okEps > 0) {
      notice.value = `一键识别部分完成：成功 ${okEps} 集、失败 ${failEps} 集（首错：${firstError}），共识别 ${total} 个（跳过 ${skippedTotal} 条），已刷新成功部分。`
    } else {
      notice.value = `一键识别失败：${failEps} 集均失败（首错：${firstError}）。`
    }
  } finally {
    extractingEpId.value = null
    extractingAll.value = false
  }
}

/** H 卡片编辑：展开编辑区（草稿快照，取消不写回）。 */
function startEdit(c: SeriesCharacter): void {
  notice.value = ''
  editingId.value = c.id
  drafts.value = {
    ...drafts.value,
    [c.id]: {
      name: c.name ?? '',
      desc: c.desc ?? '',
      aliases: (c.aliases ?? []).join(', '),
      appearance: c.appearance ?? '',
      personality: c.personality ?? '',
      relation: c.relation ?? '',
      outfit: c.outfit ?? '',
      note: c.notes ?? c.note ?? ''
    }
  }
}

function cancelEdit(charId: string): void {
  const next = { ...drafts.value }
  delete next[charId]
  drafts.value = next
  if (editingId.value === charId) editingId.value = null
}

function parseAliasesInput(raw: string): string[] {
  return raw.split(/[,，、;；]/).map((s) => s.trim()).filter((s) => s.length > 0)
}

/** H 卡片编辑保存：全量 patch + locked:true，成功说明“已锁定，自动识别不再覆盖”。 */
async function saveEdit(charId: string): Promise<void> {
  const d = drafts.value[charId]
  if (!d) return
  if (portraitBusyId.value !== null || pendingUploadCharId.value !== null) {
    notice.value = '已有立绘任务进行中，请稍候再保存。'
    return
  }
  const name = d.name.trim()
  if (!name) {
    notice.value = '角色名不能为空。'
    return
  }
  savingId.value = charId
  notice.value = ''
  const patch = {
    name,
    desc: d.desc.trim(),
    aliases: parseAliasesInput(d.aliases),
    appearance: d.appearance.trim(),
    personality: d.personality.trim(),
    relation: d.relation.trim(),
    outfit: d.outfit.trim(),
    notes: d.note.trim(),
    locked: true
  }
  try {
    await updateCharacter(seriesId.value, charId, patch)
    const c = series.characters.find((x) => x.id === charId)
    if (c) {
      c.name = patch.name
      c.desc = patch.desc
      c.aliases = [...patch.aliases]
      c.appearance = patch.appearance
      c.personality = patch.personality
      c.relation = patch.relation
      c.outfit = patch.outfit
      c.notes = patch.notes
      c.note = patch.notes
      c.locked = true
    }
    cancelEdit(charId)
    notice.value = `“${patch.name}”已保存并锁定，自动识别不再覆盖。`
  } catch (e) {
    if (isSeriesOfflineError(e)) {
      const c = series.characters.find((x) => x.id === charId)
      if (c) {
        c.name = patch.name
        c.desc = patch.desc
        c.aliases = [...patch.aliases]
        c.appearance = patch.appearance
        c.personality = patch.personality
        c.relation = patch.relation
        c.outfit = patch.outfit
        c.notes = patch.notes
        c.note = patch.notes
        c.locked = true
      }
      cancelEdit(charId)
      notice.value = '后端不可达：已在本地暂存修改（含锁定标记），联网后重保存一次。'
    } else notice.value = errText(e)
  } finally {
    savingId.value = null
  }
}
</script>

<template>
  <div class="page">
    <header class="toolbar"><div class="toolbar-inner">
      <span class="brand">角色 · {{ series.currentSeries?.title ?? seriesId }}</span>
      <span class="actions">
        <router-link class="link" :to="`/series/${encodeURIComponent(seriesId)}`">回系列</router-link>
        <router-link class="link" to="/series/new">新建系列</router-link>
      </span>
    </div></header>
    <main class="wrap">
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <p class="hint">加集后自动识别的角色请在此确认，可改名/设主图/合并/删除。</p>
      <section class="panel">
        <h2>新建角色</h2>
        <div class="grid2">
          <label class="field"><span>名</span><input v-model="newName" type="text" placeholder="如：白衣少年" maxlength="40" /></label>
          <label class="field"><span>简介</span><input v-model="newDesc" type="text" placeholder="外貌/服饰一句话" maxlength="200" /></label>
        </div>
        <button type="button" class="primary" @click="onAdd">添加角色</button>
      </section>
      <section class="panel">
        <h2>角色卡（{{ series.characters.length }}，数量不限）</h2>
        <template v-if="!series.characters.length">
          <p v-if="!extractForSeries" class="hint">尚未识别过角色：请先<router-link class="link" :to="`/series/${encodeURIComponent(seriesId)}`">回系列页</router-link>加一集并跑“AI 全规划 3/3 识别”，或在上方手动添加。</p>
          <p v-else-if="!extractForSeries.ok" class="hint">上次识别失败：{{ extractForSeries.error || '未知错误' }}；可<router-link class="link" :to="`/series/${encodeURIComponent(seriesId)}`">回系列页</router-link>重试，或在上方手动添加。</p>
          <p v-else class="hint">识别完成但为 0 个角色（跳过 {{ extractForSeries.skipped }} 条）：请在上方手动添加。</p>
          <p class="hint">也可回系列页用‘从整本小说识别角色’一次导入全文（超长自动分段合并），之后加集默认不再自动识别。</p>
          <div class="btn-row">
            <button
              type="button" class="primary"
              :disabled="extractingAll"
              @click="onExtractAll"
            >{{ extractingAll ? '识别中…' : (series.episodes.length ? `一键识别本系列（${series.episodes.length} 集）` : '一键识别本系列') }}</button>
          </div>
          <p v-if="extractingAll && extractingEpId" class="hint">正在逐集识别…请稍候（单集失败会自动跳过继续）。</p>
        </template>
        <ul class="cards">
          <li v-for="c in series.characters" :key="c.id" class="char" :class="{ main: c.is_main }">
            <div class="thumb">
              <img v-if="c.portrait_url" :src="c.portrait_url" :alt="`${c.name} 立绘`" loading="lazy" />
              <span v-else class="nopic">无立绘</span>
            </div>
            <div class="info">
              <strong>{{ c.name }} <span v-if="c.is_main" class="badge">主图</span><span v-if="c.status" class="badge" :class="{ pending: c.status === '待确认' }">{{ c.status }}</span><span v-if="c.locked" class="badge lock">已锁定</span></strong>
              <span class="desc">{{ c.desc || '—' }}</span>
              <span v-if="c.aliases && c.aliases.length" class="desc">又名：{{ c.aliases.join('、') }}</span>
              <span v-if="c.appearance" class="desc">外貌：{{ c.appearance }}</span>
              <span v-if="c.personality" class="desc">性格：{{ c.personality }}</span>
              <span v-if="c.relation" class="desc">关系：{{ c.relation }}</span>
              <span v-if="c.outfit" class="desc">服饰：{{ c.outfit }}</span>
              <span v-if="c.notes ?? c.note" class="desc">备注：{{ c.notes ?? c.note }}</span>
              <div class="btn-row">
                <button
                  v-if="c.status === '待确认'"
                  type="button" class="ghost sm"
                  :disabled="confirmingId === c.id"
                  @click="onConfirm(c.id)"
                >{{ confirmingId === c.id ? '确认中…' : '确认' }}</button>
                <button
                  type="button" class="ghost sm"
                  :disabled="portraitBusyId !== null || pendingUploadCharId !== null || cooldownLeft(c.id) > 0"
                  @click="onPortrait(c.id)"
                >{{
                  portraitBusyId !== null ? (portraitBusyId === c.id ? '生成中…' : '忙碌中…') :
                  cooldownLeft(c.id) > 0 ? `${cooldownLeft(c.id)}s后可重试` :
                  confirmPortraitId === c.id ? '确认生成？' : '生成立绘'
                }}</button>
                <button
                  type="button" class="ghost sm"
                  :disabled="portraitBusyId !== null || pendingUploadCharId !== null"
                  @click="triggerUpload(c.id)"
                >{{ portraitBusyId === c.id ? '上传中…' : (portraitBusyId !== null || pendingUploadCharId !== null ? '忙碌中…' : '上传立绘') }}</button>
                <button type="button" class="ghost sm" :disabled="portraitBusyId !== null || pendingUploadCharId !== null" @click="onSetMain(c.id)">设主图</button>
                <button type="button" class="ghost sm" @click="editingId === c.id ? cancelEdit(c.id) : startEdit(c)">{{ editingId === c.id ? '收起' : '编辑' }}</button>
                <button type="button" class="danger sm" :disabled="portraitBusyId !== null || pendingUploadCharId !== null" @click="onDelete(c.id)">删</button>
              </div>
              <div v-if="editingId === c.id && drafts[c.id]" class="edit">
                <label class="field"><span>名</span><input v-model="drafts[c.id].name" type="text" maxlength="40" placeholder="角色名" /></label>
                <label class="field"><span>简介</span><input v-model="drafts[c.id].desc" type="text" maxlength="500" placeholder="一句话简介" /></label>
                <label class="field"><span>别名（逗号分隔）</span><input v-model="drafts[c.id].aliases" type="text" maxlength="200" placeholder="如：阿雪, 小白" /></label>
                <label class="field"><span>外貌</span><input v-model="drafts[c.id].appearance" type="text" maxlength="200" placeholder="外貌特征" /></label>
                <label class="field"><span>性格</span><input v-model="drafts[c.id].personality" type="text" maxlength="200" placeholder="性格特点" /></label>
                <label class="field"><span>关系</span><input v-model="drafts[c.id].relation" type="text" maxlength="200" placeholder="与其他角色的关系" /></label>
                <label class="field"><span>服饰</span><input v-model="drafts[c.id].outfit" type="text" maxlength="200" placeholder="常服/战袍等" /></label>
                <label class="field"><span>备注</span><input v-model="drafts[c.id].note" type="text" maxlength="500" placeholder="补充备注" /></label>
                <div class="btn-row">
                  <button type="button" class="primary sm" :disabled="savingId === c.id || portraitBusyId !== null || pendingUploadCharId !== null" @click="saveEdit(c.id)">{{ savingId === c.id ? '保存中…' : '保存（并锁定）' }}</button>
                  <button type="button" class="ghost sm" :disabled="savingId === c.id" @click="cancelEdit(c.id)">取消</button>
                </div>
                <p class="hint">保存后自动锁定，自动识别不再覆盖该卡。</p>
              </div>
            </div>
          </li>
        </ul>
        <input ref="uploadInput" type="file" accept="image/png,image/jpeg,image/webp" style="display: none" @change="onUploadChange" @cancel="onUploadCancel" />
      </section>
      <section class="panel">
        <h2>合并角色</h2>
        <label class="field"><span>目标（保留）</span>
          <select v-model="mergeTarget"><option value="">请选择…</option><option v-for="c in series.characters" :key="c.id" :value="c.id">{{ c.name }}</option></select>
        </label>
        <div class="checks">
          <label v-for="c in series.characters" :key="c.id"><input type="checkbox" :checked="mergeSources.includes(c.id)" @change="toggleSource(c.id)" /> {{ c.name }}</label>
        </div>
        <button type="button" class="ghost" @click="onMerge">合并选中来源 → 目标</button>
      </section>
    </main>
  </div>
</template>

<style scoped>
.page { min-height: 100vh; background: #f5f5f7; color: #1d1d1f; }
.toolbar { position: sticky; top: 0; z-index: 10; background: rgba(255,255,255,.65); backdrop-filter: blur(20px) saturate(180%); border-bottom: 1px solid rgba(0,0,0,.08); }
.toolbar-inner { max-width: 860px; margin: 0 auto; padding: 12px 16px; display: flex; justify-content: space-between; gap: 12px; }
.brand { font-weight: 700; }
.actions { display: flex; gap: 12px; }
.wrap { max-width: 860px; margin: 0 auto; padding: 20px 16px 64px; }
.panel { background: rgba(255,255,255,.8); border: 1px solid rgba(0,0,0,.06); border-radius: 16px; padding: 16px; margin: 14px 0; }
.field { display: flex; flex-direction: column; gap: 6px; margin: 10px 0; font-size: 14px; font-weight: 600; }
input, select { font: inherit; font-weight: 400; padding: 9px 11px; border-radius: 10px; border: 1px solid rgba(0,0,0,.15); background: #fff; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.primary { font: inherit; font-weight: 700; padding: 10px 18px; border: none; border-radius: 12px; background: #0a84ff; color: #fff; cursor: pointer; }
.ghost { font: inherit; padding: 9px 14px; border-radius: 12px; border: 1px solid rgba(0,0,0,.15); background: transparent; cursor: pointer; }
.ghost.sm, .danger.sm { padding: 6px 12px; font-size: 13px; }
.primary.sm { padding: 6px 12px; font-size: 13px; }
.badge.lock { background: rgba(52,199,89,.9); }
.danger { font: inherit; border-radius: 12px; border: 1px solid rgba(255,59,48,.5); background: transparent; color: #d70015; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
.notice { padding: 10px 12px; border-radius: 10px; background: rgba(0,0,0,.05); margin: 12px 0; }
.link { color: #0a84ff; }
.hint { color: #6e6e73; font-size: 13px; }
.cards { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
.char { display: flex; gap: 10px; border: 1px solid rgba(0,0,0,.08); border-radius: 12px; padding: 10px; background: rgba(255,255,255,.6); }
.char.main { border-color: #0a84ff; }
.thumb { width: 72px; height: 96px; border-radius: 8px; background: rgba(0,0,0,.06); overflow: hidden; flex: none; display: flex; align-items: center; justify-content: center; }
.thumb img { width: 100%; height: 100%; object-fit: cover; }
.nopic { font-size: 12px; color: #8e8e93; }
.info { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.desc { font-size: 13px; color: #6e6e73; }
.badge { font-size: 11px; background: #0a84ff; color: #fff; border-radius: 999px; padding: 1px 8px; }
.badge.pending { background: rgba(255,159,10,.9); }
.edit { margin-top: 8px; border-top: 1px dashed rgba(0,0,0,.15); padding-top: 8px; display: flex; flex-direction: column; gap: 2px; }
.edit .field { margin: 6px 0; }
.checks { display: flex; gap: 12px; flex-wrap: wrap; margin: 8px 0; font-size: 14px; }
@media (max-width: 560px) { .grid2 { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) { .page { background: #000; color: #f5f5f7; } .toolbar { background: rgba(20,20,22,.6); } .panel, .char { background: rgba(28,28,30,.8); } input, select { background: #1c1c1e; color: #f5f5f7; } }
</style>
