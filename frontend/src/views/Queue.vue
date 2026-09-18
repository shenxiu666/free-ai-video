<template>
  <section class="queue" aria-label="渲染队列">
    <header class="q-head">
      <div>
        <h1 class="q-title">渲染队列</h1>
        <p class="q-sub">任务进度 · SSE 日志 · 断点续跑 / 单镜重试</p>
      </div>
      <span class="conn" :class="queue.connected ? 'on' : 'off'" role="status">
        <i class="conn-dot" aria-hidden="true" />
        {{ queue.connected ? '已连接' : '未连接' }}
      </span>
      <span v-if="queue.rendering" class="conn run" role="status">
        <i class="conn-dot" aria-hidden="true" />
        渲染中
      </span>
    </header>

    <div class="card controls">
      <label class="field">
        <span>系列（GET /api/series，无后端降级本地索引）</span>
        <select v-model="seriesSelect" @change="onSeriesChange">
          <option value="">兼容模式：不选系列（用下方单剧名）</option>
          <option v-for="s in queue.seriesList" :key="s.id" :value="s.id">{{ s.title }}</option>
        </select>
      </label>
      <div v-if="queue.seriesEpisodes.length" class="ep-capsules" role="listbox" aria-label="集列表">
        <button
          v-for="ep in queue.seriesEpisodes"
          :key="ep.id"
          type="button"
          role="option"
          :aria-selected="queue.selectedEpId === ep.id"
          class="ep-cap"
          :class="{ active: queue.selectedEpId === ep.id }"
          @click="onSelectEp(ep.id)"
        >
          <span class="ep-t">{{ ep.title }}</span>
          <span class="ep-m">{{ queue.epProgress[ep.id] ?? 0 }}% · {{ queue.epStatus[ep.id] ?? '待排' }}{{ ep.planned_seconds != null ? ` · ${ep.planned_seconds}s` : '' }}</span>
        </button>
      </div>
      <label class="field">
        <span>剧名（兼容模式：老单剧名输入，系列模式下自动填集级 key）</span>
        <input v-model="dramaName" placeholder="如 demo" inputmode="text" @keyup.enter="onConnect" />
      </label>
      <div class="btn-row">
        <button type="button" class="btn primary" @click="onConnect">连接</button>
        <button type="button" class="btn accent" :disabled="!queue.connected" @click="onStart">
          开始渲染
        </button>
        <button type="button" class="btn accent" :disabled="!queue.selectedEpId" @click="onRenderEp">
          渲染本集
        </button>
        <button type="button" class="btn accent" :disabled="!queue.seriesEpisodes.length || queue.seriesRendering" @click="onRenderAll">
          {{ queue.seriesRendering ? '整剧渲染中…' : '渲染整剧（按集串行）' }}
        </button>
        <button type="button" class="btn" :disabled="!dramaName.trim() && !queue.name" @click="onResume">
          断点续跑
        </button>
        <button type="button" class="btn ghost" :disabled="!queue.connected" @click="onDisconnect">断开</button>
      </div>
      <p class="hint">连接只看进度；“开始渲染”才真正开跑（图→视频→配音→合成，日志实时刷）。单镜失败点该镜“重试”只重跑该镜。系列模式：SSE 仍按当前集订阅，下方 clip 只看该集；系列聚合进度靠轮询汇总。</p>
    </div>

    <div class="card">
      <div class="overall">
        <span>总进度</span>
        <strong>{{ overallPct }}%</strong>
      </div>
      <div
        class="progress"
        role="progressbar"
        :aria-valuenow="overallPct"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-label="总进度"
      >
        <div class="fill" :style="{ width: overallPct + '%' }" />
      </div>
      <p v-if="!queue.clipsState.length" class="empty">
        {{ queue.lastRefresh === 'not-found'
          ? '后端无该剧：检查剧名是否打错，或先去分镜表点“保存全部”同步，同步后轮询会自动接上。'
          : '暂无分镜：输入剧名后点「连接」，无后端时会自动进入本地演示进度。' }}
      </p>
      <div class="final-banner" role="status" aria-label="成片状态">
        <template v-if="hasFinal">
          <strong class="final-title">成片：{{ latestFinal }}</strong>
          <span class="hint">{{ finalExists ? '（已同步 final.mp4）' : '（未同步 final.mp4）' }}</span>
          <span v-if="muxDetail" class="hint">｜配音 {{ muxDetail.dub_ok }}/{{ muxDetail.dub_total }}轨<span v-if="muxDetail.missing_dubs.length" class="final-warn">，缺：{{ muxDetail.missing_dubs.join(',') }}</span><span v-if="muxDetail.missing_srts.length" class="final-warn">｜字幕缺：{{ muxDetail.missing_srts.join(',') }}</span></span>
        </template>
        <p v-else class="hint final-empty">暂无成片：视频齐后自动合成</p>
      </div>
      <ul class="clips">
        <li v-for="clip in queue.clipsState" :key="clip.id">
          <article
            class="clip"
            :class="{ failed: isClipFailed(clip), pressed: pressedId === clip.id }"
            @click="openSheet(clip.id)"
            @pointerdown="pressedId = clip.id"
            @pointerup="pressedId = null"
            @pointerleave="pressedId = null"
          >
            <header class="clip-head">
              <span class="clip-id">{{ clip.id }}</span>
              <span class="clip-pct">{{ clipPct(clip) }}%</span>
            </header>
            <ol class="stages">
              <li v-for="st in STAGES" :key="st.key" class="stage">
                <i class="dot" :class="'s-' + clip[st.key]" aria-hidden="true" />
                <span class="stage-name">{{ st.label }}</span>
                <span class="stage-state">{{ stateText(clip[st.key]) }}</span>
                <button
                  v-if="clip[st.key] === 'failed'"
                  type="button"
                  class="btn retry"
                  @click.stop="doRetry(clip.id)"
                  @pointerdown.stop="pressRetry(clip.id)"
                >
                  重试
                </button>
              </li>
            </ol>
            <p v-if="clip.message" class="clip-msg">{{ clip.message }}</p>
          </article>
        </li>
      </ul>
    </div>

    <div class="card">
      <div class="log-bar">
        <strong>SSE 日志</strong>
        <label class="pause"><input v-model="paused" type="checkbox" /> 暂停滚屏</label>
        <button type="button" class="btn ghost sm" @click="queue.clearLogs()">清空</button>
      </div>
      <div ref="logBox" class="logs" aria-live="polite">
        <p v-for="(l, i) in queue.logs" :key="i" class="log" :class="'lv-' + l.level">
          <span class="t">{{ l.time }}</span><span class="m">{{ l.msg }}</span>
        </p>
        <p v-if="!queue.logs.length" class="empty">等待日志…</p>
      </div>
    </div>

    <!-- Sheet 式详情弹层：div 实现，进入/退出同一条路径（对称 transition） -->
    <Transition name="sheet">
      <div v-if="selected" class="sheet-mask" @click="closeSheet">
        <div
          class="sheet"
          role="dialog"
          aria-modal="true"
          :aria-label="'分镜 ' + (selected as ClipState).id + ' 详情'"
          @click.stop
        >
          <div class="grab" aria-hidden="true" />
          <h2 class="sheet-title">{{ (selected as ClipState).id }}</h2>
          <ol class="stages">
            <li v-for="st in STAGES" :key="st.key" class="stage">
              <i class="dot" :class="'s-' + (selected as ClipState)[st.key]" aria-hidden="true" />
              <span class="stage-name">{{ st.label }}</span>
              <span class="stage-state">{{ stateText((selected as ClipState)[st.key]) }}</span>
            </li>
          </ol>
          <p v-if="(selected as ClipState).message" class="clip-msg">{{ (selected as ClipState).message }}</p>
          <div class="btn-row">
            <button type="button" class="btn primary" @click="doRetry((selected as ClipState).id)">单镜重试</button>
            <button type="button" class="btn" @click="onResume">断点续跑</button>
            <button type="button" class="btn ghost" @click="closeSheet">关闭</button>
          </div>
        </div>
      </div>
    </Transition>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQueueStore, STAGES, type ClipState, type StageStatus } from '../stores/queue'
import { episodeDramaKey } from '../api/series'

const queue = useQueueStore()
const route = useRoute()
const dramaName = ref(queue.name || '')
const seriesSelect = ref('')
const paused = ref(false)
const pressedId = ref<string | null>(null)
const selectedId = ref<string | null>(null)
const logBox = ref<HTMLDivElement | null>(null)

onMounted(() => {
  void queue.loadSeriesIndex().then(() => {
    const qSeries = String(route.query.series ?? '')
    const qEp = String(route.query.ep ?? '')
    const qDrama = String(route.query.drama ?? '')
    if (qSeries) {
      seriesSelect.value = qSeries
      void queue.selectSeriesForQueue(qSeries).then(() => {
        if (qEp && queue.seriesEpisodes.some((e) => e.id === qEp)) void queue.selectEpisode(qEp)
        else if (qDrama) {
          dramaName.value = qDrama
          void queue.connect(qDrama)
        }
        dramaName.value = queue.name || qDrama
      })
    } else if (qDrama) {
      dramaName.value = qDrama
      void queue.connect(qDrama)
    }
  })
})

/** 系列切换：集胶囊默认选中第一集并 connect（key 用 episode 级）。 */
function onSeriesChange(): void {
  if (!seriesSelect.value) {
    queue.seriesId = ''
    queue.seriesEpisodes = []
    queue.selectedEpId = null
    return
  }
  void queue.selectSeriesForQueue(seriesSelect.value).then(() => {
    dramaName.value = queue.name
  })
}

/** 集胶囊点击：切换 selectedEp，下方 clip/SSE 只看该集。 */
function onSelectEp(epId: string): void {
  void queue.selectEpisode(epId).then(() => {
    dramaName.value = queue.name
  })
}

/** 渲染本集：复用 startRender（集级 key）。 */
function onRenderEp(): void {
  const ep = queue.seriesEpisodes.find((e) => e.id === queue.selectedEpId)
  if (ep) dramaName.value = episodeDramaKey(ep)
  void queue.renderCurrentEpisode()
}

/** 渲染整剧（按集串行）。 */
function onRenderAll(): void {
  void queue.renderSeriesAll()
}

const selected = computed<ClipState | null>(
  () => queue.clipsState.find((c) => c.id === selectedId.value) ?? null
)

function stageScore(s: StageStatus): number {
  if (s === 'done') return 1
  if (s === 'doing') return 0.5
  return 0
}

function clipPct(c: ClipState): number {
  // 后端若下发真实 progress(0-100) 优先采用，否则按四态折算
  if (typeof c.progress === 'number' && Number.isFinite(c.progress) && c.progress > 0) {
    return Math.min(100, Math.max(0, Math.round(c.progress)))
  }
  return Math.round(((stageScore(c.image) + stageScore(c.video) + stageScore(c.tts) + stageScore(c.mux)) / 4) * 100)
}

const overallPct = computed(() => {
  if (!queue.clipsState.length) return 0
  const sum = queue.clipsState.reduce((a, c) => a + clipPct(c), 0)
  return Math.round(sum / queue.clipsState.length)
})

/** 成片横幅：finals 最后一条为最新版；缺字段（无后端/旧后端）时显示“暂无成片”不报错 */
const finals = computed(() => queue.finalInfo.finals ?? [])
const hasFinal = computed(() => finals.value.length > 0)
const latestFinal = computed(() => (hasFinal.value ? finals.value[finals.value.length - 1].file : ''))
const finalExists = computed(() => queue.finalInfo.final_exists)
const muxDetail = computed(() => {
  const m = queue.finalInfo.mux_detail as unknown as Record<string, unknown> | null
  if (!m || typeof m !== 'object') return null
  return {
    dub_ok: typeof m['dub_ok'] === 'number' ? (m['dub_ok'] as number) : 0,
    dub_total: typeof m['dub_total'] === 'number' ? (m['dub_total'] as number) : 0,
    missing_dubs: Array.isArray(m['missing_dubs']) ? (m['missing_dubs'] as string[]) : [],
    missing_srts: Array.isArray(m['missing_srts']) ? (m['missing_srts'] as string[]) : [],
  }
})

function isClipFailed(c: ClipState): boolean {
  return c.image === 'failed' || c.video === 'failed' || c.tts === 'failed' || c.mux === 'failed'
}

function stateText(s: StageStatus): string {
  if (s === 'doing') return '进行中'
  if (s === 'done') return '完成'
  if (s === 'failed') return '失败'
  return '待排'
}

function onConnect(): void {
  void queue.connect(dramaName.value)
}

function onStart(): void {
  if (!dramaName.value.trim()) dramaName.value = queue.name
  void queue.startRender()
}

function onResume(): void {
  const n = dramaName.value.trim() || queue.name
  dramaName.value = n
  queue.resume(n)
}

function onDisconnect(): void {
  queue.disconnect()
}

/** pointerdown 即给出按压反馈（pressed 高亮 + 缩放），动作本身走 click，保证键盘可达。 */
function pressRetry(id: string): void {
  pressedId.value = id
}

function doRetry(id: string): void {
  pressedId.value = null
  void queue.retryClip(id)
}

function openSheet(id: string): void {
  selectedId.value = id
}

function closeSheet(): void {
  selectedId.value = null
}

// SSE 日志自动滚底；勾选暂停后保持用户阅读位置。
watch(
  () => queue.logs.length,
  async () => {
    if (paused.value || !logBox.value) return
    await nextTick()
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  }
)
</script>

<style scoped>
/* Apple 风格：spring 曲线只做 transform/width/opacity，避免 layout 抖动 */
.queue {
  --spring: cubic-bezier(0.32, 1.35, 0.42, 1);
  --ink: #1d1d1f;
  --sub: rgba(29, 29, 31, 0.6);
  --card: rgba(255, 255, 255, 0.72);
  --edge: rgba(0, 0, 0, 0.08);
  --blue: #0a84ff;
  --green: #30d158;
  --red: #ff453a;
  --amber: #ff9f0a;
  --gray: #8e8e93;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  color: var(--ink);
}

.q-head {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
}

.q-title {
  margin: 0;
  font-size: clamp(1.4rem, 1.1rem + 2vw, 2rem);
  line-height: 1.1;
  letter-spacing: -0.02em;
  font-weight: 700;
}

.q-sub {
  margin: 0.25rem 0 0;
  font-size: 0.85rem;
  color: var(--sub);
}

.conn {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.8rem;
  padding: 0.3rem 0.7rem;
  border-radius: 999px;
  background: var(--card);
  border: 1px solid var(--edge);
  white-space: nowrap;
}

.conn-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--gray);
}

.conn.on .conn-dot {
  background: var(--green);
  animation: pulse 1.6s ease-in-out infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.55;
    transform: scale(0.8);
  }
}

.card {
  background: var(--card);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  backdrop-filter: blur(20px) saturate(180%);
  border: 1px solid var(--edge);
  border-radius: 16px;
  padding: 0.9rem;
}

.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.7rem;
  align-items: flex-end;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.8rem;
  color: var(--sub);
  min-width: 10rem;
  flex: 1;
}

.field input {
  font: inherit;
  color: var(--ink);
  padding: 0.55rem 0.7rem;
  border-radius: 10px;
  border: 1px solid var(--edge);
  background: rgba(255, 255, 255, 0.9);
}

.field select {
  font: inherit;
  color: var(--ink);
  padding: 0.55rem 0.7rem;
  border-radius: 10px;
  border: 1px solid var(--edge);
  background: rgba(255, 255, 255, 0.9);
}

.ep-capsules {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  width: 100%;
}

.ep-cap {
  font: inherit;
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.15rem;
  padding: 0.45rem 0.8rem;
  border-radius: 999px;
  border: 1px solid var(--edge);
  background: rgba(255, 255, 255, 0.9);
  color: var(--ink);
  cursor: pointer;
  transition: transform 100ms ease, border-color 150ms ease;
}

.ep-cap:active {
  transform: scale(0.97);
}

.ep-cap.active {
  border-color: var(--blue);
  box-shadow: 0 0 0 2px rgba(10, 132, 255, 0.2);
}

.ep-t {
  font-size: 0.82rem;
  font-weight: 700;
}

.ep-m {
  font-size: 0.72rem;
  color: var(--sub);
  font-variant-numeric: tabular-nums;
}

.btn-row {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.btn {
  font: inherit;
  font-size: 0.85rem;
  padding: 0.55rem 1rem;
  border-radius: 999px;
  border: 1px solid var(--edge);
  background: rgba(255, 255, 255, 0.9);
  color: var(--ink);
  cursor: pointer;
  transition: transform 100ms ease, opacity 100ms ease, background 150ms ease;
  will-change: transform, opacity;
}

.btn:active {
  transform: scale(0.97);
}

.btn.primary {
  background: var(--blue);
  border-color: transparent;
  color: #fff;
}

.btn.ghost {
  background: transparent;
}

.btn.accent {
  background: var(--green);
  border-color: transparent;
  color: #fff;
}

.hint {
  margin: 0.55rem 0 0;
  font-size: 0.78rem;
  color: var(--sub);
}

.conn.run {
  margin-left: 0.4rem;
}

.conn.run .conn-dot {
  background: var(--green);
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}

.btn.sm {
  padding: 0.3rem 0.7rem;
  font-size: 0.78rem;
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn.retry {
  margin-left: auto;
  background: var(--red);
  border-color: transparent;
  color: #fff;
  padding: 0.3rem 0.8rem;
  font-size: 0.78rem;
}

.overall {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 0.85rem;
  color: var(--sub);
  margin-bottom: 0.45rem;
}

.overall strong {
  color: var(--ink);
  font-size: 1rem;
}

.progress {
  height: 10px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

.fill {
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--blue), #64d2ff);
  transition: width 0.55s var(--spring);
  will-change: width;
}

.empty {
  font-size: 0.82rem;
  color: var(--sub);
}

.final-banner {
  margin-top: 0.7rem;
  border-top: 1px solid var(--edge);
  padding-top: 0.7rem;
  font-size: 0.82rem;
  line-height: 1.6;
}

.final-banner .hint {
  margin: 0;
}

.final-title {
  color: var(--ink);
  font-weight: 700;
  word-break: break-all;
}

.final-warn {
  color: var(--amber);
}

.final-empty {
  margin: 0;
}

.clips {
  list-style: none;
  margin: 0.8rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.clip {
  border: 1px solid var(--edge);
  border-radius: 12px;
  padding: 0.7rem 0.8rem;
  background: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  transition: transform 120ms ease, border-color 150ms ease;
  will-change: transform;
}

.clip:active,
.clip.pressed {
  transform: scale(0.99);
}

.clip.failed {
  border-color: var(--red);
}

.clip-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.4rem;
}

.clip-id {
  font-weight: 700;
  letter-spacing: -0.01em;
}

.clip-pct {
  font-size: 0.8rem;
  color: var(--sub);
  font-variant-numeric: tabular-nums;
}

.stages {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 0.35rem;
}

.stage {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.82rem;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  flex: none;
  transition: background 200ms ease, transform 0.45s var(--spring);
}

.dot.s-pending {
  background: var(--gray);
  opacity: 0.55;
}

.dot.s-doing {
  background: var(--blue);
  animation: pulse 1.2s ease-in-out infinite;
}

.dot.s-done {
  background: var(--green);
}

.dot.s-failed {
  background: var(--red);
  transform: scale(1.25);
}

.stage-name {
  color: var(--ink);
}

.stage-state {
  color: var(--sub);
  font-size: 0.76rem;
}

.clip-msg {
  margin: 0.45rem 0 0;
  font-size: 0.78rem;
  color: var(--sub);
}

.log-bar {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  margin-bottom: 0.5rem;
  font-size: 0.85rem;
}

.pause {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.8rem;
  color: var(--sub);
}

.logs {
  max-height: 16rem;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.82);
  border-radius: 10px;
  padding: 0.6rem 0.7rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.75rem;
  line-height: 1.55;
}

.log {
  margin: 0;
  display: flex;
  gap: 0.6rem;
  color: #e5e5ea;
  word-break: break-all;
}

.log .t {
  color: #98989f;
  flex: none;
}

.log.lv-warn .m {
  color: var(--amber);
}

.log.lv-error .m {
  color: var(--red);
}

/* Sheet：进入/退出同一条路径（遮罩淡入淡出 + 面板同曲线滑入滑出） */
.sheet-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  z-index: 50;
}

.sheet {
  width: min(32rem, 100%);
  max-height: 80vh;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.92);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  backdrop-filter: blur(20px) saturate(180%);
  border-radius: 20px 20px 0 0;
  padding: 0.6rem 1rem 1.2rem;
}

.grab {
  width: 2.5rem;
  height: 4px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.2);
  margin: 0.2rem auto 0.6rem;
}

.sheet-title {
  margin: 0 0 0.6rem;
  font-size: 1.1rem;
  letter-spacing: -0.01em;
}

.sheet .btn-row {
  margin-top: 0.9rem;
}

.sheet-enter-active,
.sheet-leave-active {
  transition: opacity 0.3s ease;
}

.sheet-enter-active .sheet,
.sheet-leave-active .sheet {
  transition: transform 0.38s var(--spring);
}

.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}

.sheet-enter-from .sheet,
.sheet-leave-to .sheet {
  transform: translateY(100%);
}

@media (prefers-reduced-motion: reduce) {
  .conn.on .conn-dot,
  .conn.run .conn-dot,
  .dot.s-doing {
    animation: none;
  }
  .fill,
  .clip,
  .dot,
  .sheet,
  .sheet-enter-active,
  .sheet-leave-active,
  .btn {
    transition-duration: 0.01ms;
  }
  .btn:active,
  .clip.pressed {
    transform: none;
  }
}

@media (prefers-color-scheme: dark) {
  .queue {
    --ink: #f5f5f7;
    --sub: rgba(245, 245, 247, 0.65);
    --card: rgba(28, 28, 30, 0.72);
    --edge: rgba(255, 255, 255, 0.14);
  }
  .sheet {
    background: rgba(28, 28, 30, 0.92);
    color: var(--ink);
  }
}

.queue input:focus-visible,
.queue select:focus-visible,
.queue .btn:focus-visible {
  outline: 2px solid #0a84ff;
  outline-offset: 2px;
}
</style>
