<!--
  Builder B · 新建剧页
  兼容层说明（A 地基未到时保证 build 照过）：
  - 未静态 import @/components（PressButton/GlassBar）与 @/motion/springs.ts，
    全部用原生 button/div + CSS 过渡实现；A 落地后可把 .primary 换成 PressButton、
    .toolbar-inner 换成 GlassBar，把 entered 过渡换成 springTo/project。
  - 只用相对导入（../stores/drama、../api/drama），不依赖 @ 别名是否就绪。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { useDramaStore, listLocalDrafts, deleteLocalDraft, type LocalDraftInfo } from '../stores/drama';
import { fetchModels, pickTextConfig } from '../api/drama';

type ModelsState = 'checking' | 'ready' | 'missing' | 'unreachable';
type TotalPreset = '60' | '90' | '120' | 'custom';

const router = useRouter();
const store = useDramaStore();

const STYLES = ['电影感写实', '国风水墨', '赛博朋克', '日式动漫', '港风复古', '极简扁平'];

const title = ref('');
const brief = ref('');
const sourceText = ref('');
const totalPreset = ref<TotalPreset>('60');
const customTotal = ref<number>(60);
const aspect = ref<'9:16' | '16:9'>('9:16');
const style = ref(STYLES[0]);
const clipSeconds = ref<number>(8);

const modelsState = ref<ModelsState>('checking');
const textHint = ref('');
const formError = ref('');
const submitting = ref(false);
const entered = ref(false);
const drafts = ref<LocalDraftInfo[]>([]);

function refreshDrafts(): void {
  drafts.value = listLocalDrafts();
}

function openDraft(d: LocalDraftInfo): void {
  void router.push({ path: '/shots', query: { name: d.name, local: '1' } });
}

function removeDraft(d: LocalDraftInfo): void {
  deleteLocalDraft(d.name);
  refreshDrafts();
}

const totalSeconds = computed(() =>
  totalPreset.value === 'custom' ? Math.floor(customTotal.value || 0) : Number(totalPreset.value),
);
const blockedByText = computed(() => modelsState.value === 'missing');

onMounted(() => {
  // 本地 fallback 的“spring(bounce0 duration0.4)”：CSS 过渡天然可中断、可逆；
  // A 落地 motion/springs.ts 后可改为 springTo(project(...))。
  requestAnimationFrame(() => {
    entered.value = true;
  });
  refreshDrafts();
  void checkTextModel();
});

async function checkTextModel(): Promise<void> {
  modelsState.value = 'checking';
  textHint.value = '';
  try {
    const data = await fetchModels();
    const text = pickTextConfig(data);
    if (!text) {
      modelsState.value = 'unreachable';
      textHint.value =
        '未能识别 /models 返回结构：允许先本地建剧，提交前请到“密钥池+设置”确认文本模型已配置。';
      return;
    }
    if (!text.provider || !text.model) {
      modelsState.value = 'missing';
      textHint.value =
        '文本模型未配置（提供商 / 模型留空）：请先去“密钥池+设置”页填写，再回来分解。本次不会发出任何请求。';
    } else {
      modelsState.value = 'ready';
      textHint.value = `文本模型就绪：${text.provider} / ${text.model}`;
    }
  } catch (e) {
    modelsState.value = 'unreachable';
    textHint.value = `连不上后端 /models（${e instanceof Error ? e.message : String(e)}）：允许先本地建剧，联网后到设置页补配文本模型。`;
  }
}

async function onSubmit(): Promise<void> {
  formError.value = '';
  // 内联拒绝：text 留空 → 不发请求，直接指引去 /keys
  if (blockedByText.value) {
    formError.value = '文本模型未配置：请先去 /keys 设置页填写提供商 / 模型，本次未发请求。';
    return;
  }
  if (!title.value.trim()) {
    formError.value = '请填写题材（剧名）。';
    return;
  }
  if (!Number.isFinite(totalSeconds.value) || totalSeconds.value < 60) {
    formError.value = `总时长下限 60s，当前 ${String(totalSeconds.value)}s 不合法。`;
    return;
  }
  if (!Number.isFinite(clipSeconds.value) || clipSeconds.value < 4 || clipSeconds.value > 12) {
    formError.value = '单镜时长需在 4-12s 之间。';
    return;
  }
  submitting.value = true;
  try {
    const result = await store.newDrama({
      title: title.value.trim(),
      total_seconds: totalSeconds.value,
      aspect: aspect.value,
      clip_seconds: String(clipSeconds.value),
      style: style.value,
      brief: brief.value.trim(),
    });
    const query: Record<string, string> = { name: title.value.trim() };
    if (!result.remote) query['local'] = '1';
    await router.push({ path: '/shots', query });
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e);
  } finally {
    submitting.value = false;
  }
}

/** AI 分解：原文 → 分镜初稿（后端不落盘，去分镜表检查修改后再保存）。 */
async function onBreakdown(): Promise<void> {
  formError.value = '';
  if (blockedByText.value) {
    formError.value = '文本模型未配置：请先去 /keys 设置页填写提供商 / 模型，本次未发请求。';
    return;
  }
  if (!title.value.trim()) {
    formError.value = '请填写题材（剧名）。';
    return;
  }
  if (!(sourceText.value || '').trim()) {
    formError.value = '请粘贴小说/剧本原文后再分解（超长会自动截断前 12000 字）。';
    return;
  }
  if (!Number.isFinite(totalSeconds.value) || totalSeconds.value < 60) {
    formError.value = `总时长下限 60s，当前 ${String(totalSeconds.value)}s 不合法。`;
    return;
  }
  if (!Number.isFinite(clipSeconds.value) || clipSeconds.value < 4 || clipSeconds.value > 12) {
    formError.value = '单镜时长需在 4-12s 之间。';
    return;
  }
  submitting.value = true;
  try {
    const result = await store.breakdown({
      name: title.value.trim(),
      total_seconds: totalSeconds.value,
      aspect: aspect.value,
      clip_seconds: String(clipSeconds.value),
      style: style.value,
      source_text: sourceText.value,
    });
    formError.value = '';
    await router.push({ path: '/shots', query: { name: title.value.trim(), local: '1', fresh: result.message } });
  } catch (e) {
    formError.value = e instanceof Error ? e.message : String(e);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="page" :class="{ entered }">
    <header class="toolbar">
      <div class="toolbar-inner">
        <span class="brand">新建短剧</span>
        <router-link class="link" to="/keys">密钥池+设置</router-link>
      </div>
    </header>

    <main class="card">
      <h1>新建短剧</h1>
      <p class="sub">粘贴小说/剧本，AI 自动分解分镜（可在分镜表修改）· 60s 起步 · 文本模型需先在设置页配好</p>

      <div v-if="modelsState === 'checking'" class="notice">正在检查文本模型（GET /models）…</div>
      <div v-else-if="modelsState === 'missing'" class="notice error">
        {{ textHint }}
        <router-link class="link" to="/keys">去 /keys 设置</router-link>
      </div>
      <div v-else-if="modelsState === 'unreachable'" class="notice warn">{{ textHint }}</div>
      <div v-else class="notice ok">{{ textHint }}</div>

      <form @submit.prevent="onSubmit" novalidate>
        <label class="field">
          <span>题材（剧名）</span>
          <input v-model="title" type="text" placeholder="如：重生之我在免费池修仙" maxlength="60" />
        </label>

        <label class="field">
          <span>大纲</span>
          <textarea v-model="brief" rows="2" placeholder="一句话故事梗概（离线建空剧时用；AI 分解以原文为准）" maxlength="2000"></textarea>
        </label>

        <label class="field">
          <span>小说 / 剧本原文（AI 分解用，超长自动截断前 12000 字）</span>
          <textarea v-model="sourceText" rows="10" placeholder="把小说章节或剧本正文粘贴到这里，AI 会按总时长切分成镜并写好每镜台词与提示词" maxlength="50000"></textarea>
        </label>

        <fieldset class="field">
          <legend>总时长（下限 60s）</legend>
          <div class="radios">
            <label><input v-model="totalPreset" type="radio" value="60" /> 60s</label>
            <label><input v-model="totalPreset" type="radio" value="90" /> 90s</label>
            <label><input v-model="totalPreset" type="radio" value="120" /> 120s</label>
            <label><input v-model="totalPreset" type="radio" value="custom" /> 自定义</label>
            <input
              v-if="totalPreset === 'custom'"
              v-model.number="customTotal"
              type="number"
              min="60"
              step="1"
              aria-label="自定义总时长（秒）"
            />
          </div>
        </fieldset>

        <fieldset class="field">
          <legend>画幅</legend>
          <div class="radios">
            <label><input v-model="aspect" type="radio" value="9:16" /> 竖屏 9:16</label>
            <label><input v-model="aspect" type="radio" value="16:9" /> 横屏 16:9</label>
          </div>
        </fieldset>

        <div class="row">
          <label class="field">
            <span>风格</span>
            <select v-model="style">
              <option v-for="s in STYLES" :key="s" :value="s">{{ s }}</option>
            </select>
          </label>
          <label class="field">
            <span>单镜时长（秒，4-12，默认 8）</span>
            <input v-model.number="clipSeconds" type="number" min="4" max="12" step="1" />
          </label>
        </div>

        <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>

        <div class="btn-row">
          <button class="primary" type="button" :disabled="submitting || blockedByText" @click="onBreakdown">
            {{ submitting ? 'AI 分解中（约 1 分钟）…' : 'AI 分解并去分镜表' }}
          </button>
          <button class="ghost" type="submit" :disabled="submitting">
            离线建空剧
          </button>
        </div>
        <p class="hint">AI 分解需后端在线+文本模型就绪；无后端时可用“离线建空剧”先搭架子。</p>
      </form>
    </main>

    <section class="card">
      <h2>本地草稿（{{ drafts.length }}）</h2>
      <p v-if="!drafts.length" class="sub">暂无：AI 分解或离线建剧后会自动存一份在浏览器本地。</p>
      <ul v-else class="drafts">
        <li v-for="d in drafts" :key="d.name" class="draft">
          <button type="button" class="linklike" @click="openDraft(d)" :title="`打开 ${d.name}`">
            {{ d.name }}
          </button>
          <span class="meta">{{ d.clips }} 镜 · {{ d.total_seconds }}s</span>
          <button type="button" class="mini-danger" @click="removeDraft(d)">删</button>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  opacity: 0;
  transform: translateY(8px);
  background: #f5f5f7;
  color: #1d1d1f;
}
/* spring(bounce0 duration0.4) 本地 fallback：0.4s iOS 风格缓动，可中断可逆 */
.page.entered {
  opacity: 1;
  transform: none;
  transition:
    opacity 0.4s ease,
    transform 0.4s cubic-bezier(0.32, 0.72, 0, 1);
}
.toolbar {
  position: sticky;
  top: 0;
  z-index: 10;
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  background: rgba(255, 255, 255, 0.65);
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
}
.toolbar-inner {
  max-width: 720px;
  margin: 0 auto;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.brand {
  font-weight: 700;
}
.card {
  max-width: 720px;
  margin: 24px auto 48px;
  padding: 24px;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.8);
  border: 1px solid rgba(0, 0, 0, 0.06);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.06);
}
.sub {
  color: #6e6e73;
  margin-top: -8px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 14px 0;
  border: none;
  padding: 0;
}
.field > span,
.field > legend {
  font-weight: 600;
  font-size: 14px;
}
input[type='text'],
input[type='number'],
textarea,
select {
  font: inherit;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid rgba(0, 0, 0, 0.15);
  background: #fff;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease;
}
/* 表单聚焦即时高亮 */
input:focus-visible,
textarea:focus-visible,
select:focus-visible {
  outline: 2px solid #0a84ff;
  outline-offset: 1px;
  border-color: #0a84ff;
  box-shadow: 0 0 0 4px rgba(10, 132, 255, 0.15);
}
.radios {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.radios label {
  display: flex;
  gap: 6px;
  align-items: center;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.notice {
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.05);
  margin: 12px 0;
}
.notice.ok {
  background: rgba(52, 199, 89, 0.15);
}
.notice.warn {
  background: rgba(255, 159, 10, 0.18);
}
.notice.error {
  background: rgba(255, 59, 48, 0.14);
}
.error-text {
  color: #d70015;
  font-weight: 600;
}
.link {
  color: #0a84ff;
}
.primary {
  font: inherit;
  font-weight: 700;
  width: 100%;
  padding: 12px;
  border: none;
  border-radius: 12px;
  background: #0a84ff;
  color: #fff;
  cursor: pointer;
  transition:
    transform 0.12s ease,
    background 0.2s ease,
    opacity 0.2s ease;
}
/* 按钮 pointerdown 反馈 */
.primary:active:not(:disabled) {
  transform: scale(0.97);
}
.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-row {
  display: flex;
  gap: 0.6rem;
  margin-top: 0.2rem;
}
.btn-row .primary {
  flex: 1;
}
.ghost {
  font: inherit;
  font-weight: 600;
  padding: 12px 1rem;
  border-radius: 12px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  background: transparent;
  color: inherit;
  cursor: pointer;
  transition: transform 0.12s ease, opacity 0.2s ease;
  white-space: nowrap;
}
.ghost:active:not(:disabled) {
  transform: scale(0.97);
}
.ghost:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.hint {
  margin-top: 0.6rem;
  font-size: 0.8rem;
  color: #6e6e73;
}
.card h2 {
  margin: 0 0 0.4rem;
  font-size: 1.05rem;
  letter-spacing: -0.01em;
}
.drafts {
  list-style: none;
  margin: 0.4rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.draft {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}
.linklike {
  font: inherit;
  font-weight: 700;
  background: none;
  border: none;
  padding: 0;
  color: #0a84ff;
  cursor: pointer;
}
.meta {
  font-size: 0.78rem;
  color: #6e6e73;
}
.mini-danger {
  margin-left: auto;
  font: inherit;
  font-size: 0.78rem;
  padding: 0.2rem 0.6rem;
  border-radius: 999px;
  border: 1px solid rgba(255, 69, 58, 0.5);
  background: transparent;
  color: #d70015;
  cursor: pointer;
}
@media (max-width: 560px) {
  .row {
    grid-template-columns: 1fr;
  }
}
/* 深色模式 */
@media (prefers-color-scheme: dark) {
  .page {
    background: #000;
    color: #f5f5f7;
  }
  .toolbar {
    background: rgba(20, 20, 22, 0.6);
    border-bottom-color: rgba(255, 255, 255, 0.12);
  }
  .card {
    background: rgba(28, 28, 30, 0.8);
    border-color: rgba(255, 255, 255, 0.1);
  }
  .sub {
    color: #a1a1a6;
  }
  input[type='text'],
  input[type='number'],
  textarea,
  select {
    background: #1c1c1e;
    border-color: rgba(255, 255, 255, 0.2);
    color: #f5f5f7;
  }
  .notice {
    background: rgba(255, 255, 255, 0.08);
  }
}
/* reduced-motion：关过渡/动画 */
@media (prefers-reduced-motion: reduce) {
  .page {
    opacity: 1;
    transform: none;
  }
  .page.entered,
  .primary,
  input,
  textarea,
  select {
    transition: none;
  }
}
</style>
