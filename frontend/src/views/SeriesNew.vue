<!-- Builder M2 · 新建系列极简页：系列名/简介/风格，文本留空内联拒绝并指引去 /keys。 -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSeriesStore } from '../stores/series'
import { fetchModels, pickTextConfig } from '../api/drama'

const router = useRouter()
const series = useSeriesStore()

const STYLES = ['电影感写实', '国风水墨', '赛博朋克', '日式动漫', '港风复古', '极简扁平']

const title = ref('')
const synopsis = ref('')
const style = ref(STYLES[0] ?? '')
const formError = ref('')
const submitting = ref(false)
const textState = ref<'checking' | 'ready' | 'missing' | 'unreachable'>('checking')
const textHint = ref('')

onMounted(() => {
  void series.loadSeriesList()
  void checkText()
})

async function checkText(): Promise<void> {
  textState.value = 'checking'
  try {
    const data = await fetchModels()
    const t = pickTextConfig(data)
    if (!t) {
      textState.value = 'unreachable'
      textHint.value = '未能识别 /models 结构：可先本地建系列，AI 规划前请到设置页确认文本模型。'
      return
    }
    if (!t.provider || !t.model) {
      textState.value = 'missing'
      textHint.value = '文本模型未配置（提供商 / 模型留空）：系列可建，但 AI 规划/分镜会被拒绝，请先去“密钥池+设置”填写。本次不会发出任何请求。'
    } else {
      textState.value = 'ready'
      textHint.value = `文本模型就绪：${t.provider} / ${t.model}`
    }
  } catch (e) {
    textState.value = 'unreachable'
    textHint.value = `连不上后端 /models（${e instanceof Error ? e.message : String(e)}）：允许先本地建系列。`
  }
}

async function onSubmit(): Promise<void> {
  formError.value = ''
  if (!title.value.trim()) {
    formError.value = '请填写系列名。'
    return
  }
  submitting.value = true
  try {
    const created = await series.newSeries({ title: title.value.trim(), synopsis: synopsis.value.trim(), style: style.value })
    await router.push({ path: `/series/${encodeURIComponent(created.id)}` })
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="toolbar"><div class="toolbar-inner">
      <span class="brand">新建系列</span>
      <router-link class="link" to="/keys">密钥池+设置</router-link>
    </div></header>
    <main class="card">
      <h1>新建系列</h1>
      <p class="sub">系列下挂多集，每集独立成剧（风格到集一级，不做逐镜风格框）。</p>
      <div v-if="textState === 'missing'" class="notice error">{{ textHint }} <router-link class="link" to="/keys">去 /keys 设置</router-link></div>
      <div v-else class="notice" :class="{ ok: textState === 'ready', warn: textState === 'unreachable' }">{{ textHint || '正在检查文本模型…' }}</div>
      <form @submit.prevent="onSubmit" novalidate>
        <label class="field"><span>系列名</span>
          <input v-model="title" type="text" placeholder="如：免费池修仙传" maxlength="60" />
        </label>
        <label class="field"><span>简介（可选）</span>
          <textarea v-model="synopsis" rows="3" placeholder="一句话世界观/主线" maxlength="2000"></textarea>
        </label>
        <label class="field"><span>默认风格（可被每集覆盖）</span>
          <select v-model="style"><option v-for="s in STYLES" :key="s" :value="s">{{ s }}</option></select>
        </label>
        <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>
        <button class="primary" type="submit" :disabled="submitting">{{ submitting ? '创建中…' : '创建系列' }}</button>
      </form>
    </main>
    <section class="card">
      <h2>已有系列（{{ series.seriesList.length }}）{{ series.lastSource === 'local' ? '·本地索引' : '' }}</h2>
      <ul v-if="series.seriesList.length" class="slist">
        <li v-for="s in series.seriesList" :key="s.id">
          <router-link class="link" :to="`/series/${encodeURIComponent(s.id)}`">{{ s.title }}</router-link>
          <span class="meta">{{ s.episode_count != null ? `${s.episode_count}集 · ` : '' }}{{ s.style ?? '' }}</span>
        </li>
      </ul>
      <p v-else class="sub">暂无系列。</p>
    </section>
  </div>
</template>

<style scoped>
.page { min-height: 100vh; background: #f5f5f7; color: #1d1d1f; }
.toolbar { position: sticky; top: 0; z-index: 10; backdrop-filter: blur(20px) saturate(180%); background: rgba(255,255,255,.65); border-bottom: 1px solid rgba(0,0,0,.08); }
.toolbar-inner { max-width: 720px; margin: 0 auto; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; }
.brand { font-weight: 700; }
.card { max-width: 720px; margin: 24px auto; padding: 24px; border-radius: 16px; background: rgba(255,255,255,.8); border: 1px solid rgba(0,0,0,.06); box-shadow: 0 8px 30px rgba(0,0,0,.06); }
.sub { color: #6e6e73; }
.field { display: flex; flex-direction: column; gap: 6px; margin: 14px 0; font-size: 14px; font-weight: 600; }
input[type=text], textarea, select { font: inherit; font-weight: 400; padding: 10px 12px; border-radius: 10px; border: 1px solid rgba(0,0,0,.15); background: #fff; }
input:focus-visible, textarea:focus-visible, select:focus-visible { outline: 2px solid #0a84ff; outline-offset: 1px; }
.notice { padding: 10px 12px; border-radius: 10px; background: rgba(0,0,0,.05); margin: 12px 0; font-size: 14px; }
.notice.ok { background: rgba(52,199,89,.15); } .notice.warn { background: rgba(255,159,10,.18); } .notice.error { background: rgba(255,59,48,.14); }
.error-text { color: #d70015; font-weight: 600; }
.link { color: #0a84ff; }
.primary { font: inherit; font-weight: 700; width: 100%; padding: 12px; border: none; border-radius: 12px; background: #0a84ff; color: #fff; cursor: pointer; }
.primary:disabled { opacity: .5; cursor: not-allowed; }
.slist { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.slist li { display: flex; gap: 8px; align-items: baseline; }
.meta { font-size: 12px; color: #6e6e73; }
@media (prefers-color-scheme: dark) { .page { background: #000; color: #f5f5f7; } .toolbar { background: rgba(20,20,22,.6); } .card { background: rgba(28,28,30,.8); } input[type=text], textarea, select { background: #1c1c1e; color: #f5f5f7; } }
</style>
