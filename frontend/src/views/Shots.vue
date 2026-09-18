<!--
  Builder B · 分镜表页（编辑 script.json）
  兼容层说明：同 NewDrama.vue，原生元素 + CSS 过渡，未依赖 A 的 components/motion。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { useDramaStore } from '../stores/drama';
import type { ScriptClip } from '../stores/drama';

const route = useRoute();
const store = useDramaStore();

const name = computed(() => String(route.query.name ?? ''));
const fromLocal = computed(() => String(route.query.local ?? '') === '1');
const notice = ref('');
const entered = ref(false);
const newRef = ref('');
const newSceneRef = ref('');

const hasScript = computed(() => store.script !== null);
const total = computed(() => store.script?.total_seconds ?? 0);
const sum = computed(() => store.clipSum);
const consistent = computed(() => hasScript.value && store.durationConsistent);
const validation = computed(() => store.validate());
const clips = computed<ScriptClip[]>(() => store.script?.clips ?? []);
const refs = computed(() => store.script?.character_refs ?? []);
const sceneRefs = computed(() => store.script?.scene_refs ?? []);
const missing = computed(() => store.missingAssets ?? []);

/** M3：台词密度 = 全剧旁白+对白字数 / 总秒；与 store.validateScript 的超长规则同口径 */
const totalChars = computed(() =>
  clips.value.reduce(
    (acc, c) =>
      acc +
      [...(c.narration ?? '')].length +
      [...((c.dialogue ?? '') as string)].length,
    0,
  ),
);
const density = computed(() => (total.value > 0 ? totalChars.value / total.value : 0));
const longDialogueCount = computed(
  () =>
    clips.value.filter((c) => {
      const text = ((c.dialogue ?? '') as string).trim();
      const len = [...text].length;
      return len > 45 || (len > 30 && Number(c.duration) <= 8);
    }).length,
);
const densityText = computed(
  () => `密度 ${density.value.toFixed(1)} 字/s · 对白超长 ${longDialogueCount.value} 镜`,
);

onMounted(() => {
  requestAnimationFrame(() => {
    entered.value = true;
  });
  if (name.value) void load();
});

async function load(): Promise<void> {
  notice.value = '加载中…';
  try {
    const r = await store.loadState(name.value);
    notice.value = r.remote ? '' : r.message;
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e);
  }
}

function onAdd(): void {
  const clip = store.addClip();
  notice.value = clip ? `已加一镜 ${clip.id}（本地草稿，记得保存全部）` : '剧本为空，无法加镜';
}

function onRemove(id: string): void {
  store.removeClip(id);
  notice.value = `已删除 ${id}（本地草稿，记得保存全部；时长和若变化请检查顶部校验）`;
}

function onRetime(): void {
  store.retime();
  notice.value = '已按时长顺序重排开始秒';
}

async function onSaveClip(clip: ScriptClip): Promise<void> {
  const { id, ...patch } = clip;
  const r = await store.saveClip(id, { ...patch });
  notice.value = r.message;
}

async function onSaveAll(): Promise<void> {
  const r = await store.saveAll();
  notice.value = r.message;
}

function onAddRef(): void {
  if (!store.script) return;
  const v = newRef.value.trim();
  if (!v) return;
  if (refs.value.length >= 5) {
    notice.value = 'character_refs 最多 5 张，拒绝添加';
    return;
  }
  store.setCharacterRefs([...refs.value, v]);
  newRef.value = '';
  notice.value = '角色锚点已更新（本地草稿，记得保存全部）';
}

function onRemoveRef(index: number): void {
  if (!store.script) return;
  store.setCharacterRefs(refs.value.filter((_, i) => i !== index));
}

function onAddSceneRef(): void {
  if (!store.script) return;
  const v = newSceneRef.value.trim();
  if (!v) return;
  if (sceneRefs.value.length >= 5) {
    notice.value = 'scene_refs 最多 5 张，拒绝添加';
    return;
  }
  store.setSceneRefs([...sceneRefs.value, v]);
  newSceneRef.value = '';
  notice.value = '场景锚点已更新（本地草稿，记得保存全部）';
}

function onRemoveSceneRef(index: number): void {
  if (!store.script) return;
  store.setSceneRefs(sceneRefs.value.filter((_, i) => i !== index));
}

function onEditSceneRef(index: number, value: string): void {
  if (!store.script) return;
  const next = [...sceneRefs.value];
  next[index] = value;
  store.setSceneRefs(next);
}

/** cast / scene：逗号/顿号/空白分隔解析为 string[] */
function parseNames(raw: string): string[] {
  const out: string[] = [];
  for (const s of raw.split(/[,，、\s\n]+/)) {
    const t = s.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

function onCastInput(clip: ScriptClip, raw: string): void {
  clip.cast = parseNames(raw);
}

function onSceneInput(clip: ScriptClip, raw: string): void {
  clip.scene = parseNames(raw);
}

/** 本镜缺图项（AI 规划引用但库内尚无文件：去角色/场景页补传或 AI 生成后回填 images） */
function missingFor(clipId: string): { field: string; kind: string; planned_path: string }[] {
  return missing.value.filter((m) => m && m.clip_id === clipId);
}

function onEditRef(index: number, value: string): void {
  if (!store.script) return;
  const next = [...refs.value];
  next[index] = value;
  store.setCharacterRefs(next);
}

/** text 模式清媒体字段（切换模式后 hidden 字段有残留时用） */
function onClearMedia(clip: ScriptClip): void {
  clip.first_frame = undefined;
  clip.last_frame = undefined;
  clip.images = [];
  clip.audios = [];
  clip.videos = [];
  notice.value = `${clip.id} 的媒体字段已清空（本地草稿，记得保存本镜）`;
}

/** keyframe 模式清 images/audios 残留 */
function onClearKeyframeMedia(clip: ScriptClip): void {
  clip.images = [];
  clip.audios = [];
  clip.videos = [];
  notice.value = `${clip.id} 的 images/audios 已清空（keyframe 仅需首/尾帧）`;
}

/** reference 模式 images/audios：textarea 按行解析 */
function onLinesInput(clip: ScriptClip, field: 'images' | 'audios', raw: string): void {
  clip[field] = raw
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/** 沿用上一镜分镜图 → 本镜首帧（keyframe 链式衔接，与 runner 产物路径 images/ 统一） */
function onChainFrame(index: number): void {
  const prev = clips.value[index - 1];
  const cur = clips.value[index];
  if (!prev || !cur) return;
  const tail = (prev.last_frame || '').trim() || `images/${prev.id}.png`;
  cur.first_frame = tail;
  notice.value = `${cur.id} 的首帧已沿用上一镜分镜图（本地草稿，记得保存本镜）`;
}

function hasMediaLeft(clip: ScriptClip): boolean {
  return (
    !!(clip.first_frame || '').trim() ||
    !!(clip.last_frame || '').trim() ||
    (clip.images ?? []).length > 0 ||
    (clip.audios ?? []).length > 0 ||
    (clip.videos ?? []).length > 0
  );
}
</script>

<template>
  <div class="page" :class="{ entered }">
    <header class="toolbar">
      <div class="toolbar-inner">
        <span class="brand">分镜表 · {{ name || '（未指定剧名）' }}</span>
        <span v-if="hasScript" class="sum" :class="{ bad: !consistent }">
          总 {{ total }}s / 和 {{ sum }}s
        </span>
        <span v-if="hasScript" class="density" :title="`旁白+对白共 ${totalChars} 字`">
          {{ densityText }}
        </span>
        <button class="ghost" type="button" @click="onSaveAll">保存全部</button>
      </div>
    </header>

    <main class="wrap">
      <div v-if="fromLocal" class="notice warn">后端不可达，当前为本地草稿：编辑均先落本地，联网后点“保存全部”同步。</div>
      <div v-if="missing.length > 0" class="notice warn">
        待补图 {{ missing.length }} 处（AI 规划要图但库内尚无文件，模式已保留）：去角色/场景页补传或 AI 生成后，把回填路径粘进对应镜的参考图并保存。
        <ul class="errlist">
          <li v-for="(m, i) in missing" :key="'m' + i">{{ m.clip_id }} · {{ m.planned_path }}</li>
        </ul>
      </div>
      <div v-if="!hasScript && !store.loading" class="notice">
        {{ notice || '剧本为空：请先去新建剧，或检查 ?name= 参数。' }}
      </div>
      <div v-if="store.loading" class="notice">加载中…</div>

      <template v-if="hasScript">
        <p v-if="!consistent" class="error-text" role="alert">
          分镜时长之和（{{ sum }}s）≠ 总时长（{{ total }}s），请调整 duration 后再渲染。
        </p>

        <section class="panel">
          <h2>角色锚点（{{ refs.length }}/5）</h2>
          <ul class="reflist">
            <li v-for="(url, i) in refs" :key="i">
              <input :value="url" type="text" placeholder="https://…/charN.png" @input="onEditRef(i, ($event.target as HTMLInputElement).value)" />
              <button type="button" @click="onRemoveRef(i)">删</button>
            </li>
          </ul>
          <div class="refadd">
            <input v-model="newRef" type="text" placeholder="粘贴立绘 URL 回车添加" @keyup.enter="onAddRef" />
            <button type="button" @click="onAddRef">添加</button>
          </div>
        </section>

        <section class="panel">
          <h2>场景锚点（{{ sceneRefs.length }}/5）</h2>
          <ul class="reflist">
            <li v-for="(url, i) in sceneRefs" :key="'sc' + i">
              <input :value="url" type="text" placeholder="scenes/…png 或 https://…场景图" @input="onEditSceneRef(i, ($event.target as HTMLInputElement).value)" />
              <button type="button" @click="onRemoveSceneRef(i)">删</button>
            </li>
          </ul>
          <div class="refadd">
            <input v-model="newSceneRef" type="text" placeholder="粘贴场景图路径回车添加" @keyup.enter="onAddSceneRef" />
            <button type="button" @click="onAddSceneRef">添加</button>
          </div>
        </section>

        <section v-for="(clip, index) in clips" :key="clip.id" class="panel shot">
          <div class="shot-head">
            <label>镜号 <input :value="clip.id" type="text" class="id" readonly title="镜号不可改名（改名请删镜重建，避免保存时按镜号查找失效）" /></label>
            <label>开始秒 <input v-model.number="clip.start" type="number" min="0" step="1" /></label>
            <label>时长秒 <input v-model.number="clip.duration" type="number" min="1" step="1" /></label>
            <button type="button" class="danger" @click="onRemove(clip.id)">删除本镜</button>
          </div>

          <label class="field">
            <span>旁白（走 TTS）</span>
            <textarea v-model="clip.narration" rows="2"></textarea>
          </label>
          <label class="field">
            <span>人物对白（B 路线视频原生发声，只进 video_prompt）</span>
            <textarea v-model="clip.dialogue" rows="2" placeholder="人物原声台词，留空则本镜无对白"></textarea>
          </label>
          <label class="field">
            <span>说话人（可选）</span>
            <input v-model="clip.speaker" type="text" placeholder="如：女主 / 旁白" />
          </label>
          <label class="field">
            <span>参演角色 cast（逗号/顿号分隔，AI 已规划，可手改）</span>
            <input
              :value="(clip.cast ?? []).join('，')"
              type="text"
              placeholder="如：阿雪，师尊"
              @input="onCastInput(clip, ($event.target as HTMLInputElement).value)"
            />
          </label>
          <label class="field">
            <span>出场场景 scene（逗号/顿号分隔，AI 已规划，可手改）</span>
            <input
              :value="(clip.scene ?? []).join('，')"
              type="text"
              placeholder="如：雪夜山门"
              @input="onSceneInput(clip, ($event.target as HTMLInputElement).value)"
            />
          </label>
          <label class="field">
            <span>画面提示词（[主体+场景+风格+光照+构图+质量]）</span>
            <textarea v-model="clip.image_prompt" rows="2"></textarea>
          </label>
          <label class="field">
            <span>运镜提示词（[主体+动作+场景+运镜+光照+风格]）</span>
            <textarea v-model="clip.video_prompt" rows="2"></textarea>
          </label>

          <label class="field">
            <span>生成模式（AI 已规划，可手改）</span>
            <select v-model="clip.video_mode">
              <option value="text">纯文本</option>
              <option value="keyframe">首尾帧</option>
              <option value="reference">参考图</option>
            </select>
          </label>

          <div v-if="missingFor(clip.id).length > 0" class="notice warn">
            本镜待补图（模式已保留，补后把路径粘进参考图）：
            <ul class="errlist">
              <li v-for="(m, i) in missingFor(clip.id)" :key="'cm' + i">{{ m.planned_path }}</li>
            </ul>
          </div>

          <!-- 纯文本：隐藏全部媒体字段 -->
          <p v-if="clip.video_mode === 'text'" class="hint">纯文本模式禁止携带任何媒体字段。</p>

          <!-- 首尾帧：显首帧/尾帧，隐参考图/参考音频 -->
          <div v-if="clip.video_mode === 'keyframe'" class="mediabox">
            <label class="field">
              <span>首帧</span>
              <input v-model="clip.first_frame" type="text" placeholder="images/s01.png（上一镜分镜图）" />
            </label>
            <label class="field">
              <span>尾帧（可选，与首帧至少其一）</span>
              <input v-model="clip.last_frame" type="text" placeholder="images/s02.png" />
            </label>
            <button v-if="index > 0" type="button" class="ghost" @click="onChainFrame(index)">
              沿用上一镜尾帧
            </button>
            <button
              v-if="(clip.images ?? []).length > 0 || (clip.audios ?? []).length > 0"
              type="button"
              class="ghost"
              @click="onClearKeyframeMedia(clip)"
            >
              清空残留参考图/参考音频（首尾帧模式不需要）
            </button>
          </div>

          <!-- 参考图：显参考图/参考音频，隐首帧，禁视频 -->
          <div v-if="clip.video_mode === 'reference'" class="mediabox">
            <label class="field">
              <span>参考图（≤5，每行一个地址，用 &lt;Picture N&gt; 在提示词中指代）</span>
              <textarea
                :value="(clip.images ?? []).join('\n')"
                rows="3"
                @input="onLinesInput(clip, 'images', ($event.target as HTMLTextAreaElement).value)"
              ></textarea>
            </label>
            <label class="field">
              <span>参考音频（≤3，每行一个路径/地址）</span>
              <textarea
                :value="(clip.audios ?? []).join('\n')"
                rows="2"
                @input="onLinesInput(clip, 'audios', ($event.target as HTMLTextAreaElement).value)"
              ></textarea>
            </label>
            <p class="hint">B路线原生发声：对白不填audios，留空即可</p>
            <p v-if="(clip.videos ?? []).length > 0" class="error-text" role="alert">
              参考图模式禁止携带视频，请清空后再保存。
            </p>
          </div>

          <div class="shot-foot">
            <button
              v-if="clip.video_mode === 'text' && hasMediaLeft(clip)"
              type="button"
              class="ghost"
              @click="onClearMedia(clip)"
            >
              清空残留媒体字段
            </button>
            <button type="button" class="primary" @click="onSaveClip(clip)">保存本镜</button>
          </div>
        </section>

        <div class="actions">
          <button type="button" class="ghost" @click="onAdd">+ 加一镜</button>
          <button type="button" class="ghost" @click="onRetime">重排开始秒</button>
          <button type="button" class="primary" @click="onSaveAll">保存全部</button>
        </div>

        <section v-if="validation.errors.length > 0 || validation.warnings.length > 0" class="panel">
          <h2>校验</h2>
          <ul class="errlist">
            <li v-for="(e, i) in validation.errors" :key="'e' + i" class="error-text">{{ e }}</li>
          </ul>
          <ul>
            <li v-for="(w, i) in validation.warnings" :key="'w' + i" class="warn-text">{{ w }}</li>
          </ul>
          <p v-if="validation.ok" class="ok-text">全部错误项通过（警告不阻断渲染，但建议处理）。</p>
        </section>
      </template>

      <p v-if="notice" class="notice">{{ notice }}</p>
    </main>
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
  max-width: 860px;
  margin: 0 auto;
  padding: 12px 16px;
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}
.brand {
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sum {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: #34c759;
}
.sum.bad {
  color: #d70015;
}
.density {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  color: #6e6e73;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wrap {
  max-width: 860px;
  margin: 0 auto;
  padding: 20px 16px 64px;
}
.panel {
  background: rgba(255, 255, 255, 0.8);
  border: 1px solid rgba(0, 0, 0, 0.06);
  border-radius: 16px;
  padding: 16px;
  margin: 14px 0;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.06);
}
.shot-head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: end;
}
.shot-head label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  font-weight: 600;
}
.shot-head input {
  width: 90px;
}
.shot-head input.id {
  width: 110px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0;
  font-size: 14px;
}
.field > span {
  font-weight: 600;
}
input[type='text'],
input[type='number'],
textarea,
select {
  font: inherit;
  padding: 9px 11px;
  border-radius: 10px;
  border: 1px solid rgba(0, 0, 0, 0.15);
  background: #fff;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease;
}
input:focus-visible,
textarea:focus-visible,
select:focus-visible {
  outline: 2px solid #0a84ff;
  outline-offset: 1px;
  border-color: #0a84ff;
  box-shadow: 0 0 0 4px rgba(10, 132, 255, 0.15);
}
.hint {
  color: #6e6e73;
  font-size: 13px;
}
.mediabox {
  border-left: 3px solid #0a84ff;
  padding-left: 12px;
  margin: 8px 0;
}
.shot-foot,
.actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  flex-wrap: wrap;
  margin-top: 10px;
}
.actions {
  margin: 18px 0;
}
.primary {
  font: inherit;
  font-weight: 700;
  padding: 10px 18px;
  border: none;
  border-radius: 12px;
  background: #0a84ff;
  color: #fff;
  cursor: pointer;
  transition:
    transform 0.12s ease,
    background 0.2s ease;
}
.primary:active {
  transform: scale(0.97);
}
.ghost {
  font: inherit;
  padding: 9px 14px;
  border-radius: 12px;
  border: 1px solid rgba(0, 0, 0, 0.15);
  background: transparent;
  color: inherit;
  cursor: pointer;
  transition: transform 0.12s ease;
}
.ghost:active {
  transform: scale(0.97);
}
.danger {
  font: inherit;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid rgba(255, 59, 48, 0.5);
  background: transparent;
  color: #d70015;
  cursor: pointer;
}
.danger:active {
  transform: scale(0.97);
}
.reflist {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.reflist li {
  display: flex;
  gap: 8px;
}
.reflist input,
.refadd input {
  flex: 1;
}
.refadd {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.error-text {
  color: #d70015;
  font-weight: 600;
}
.warn-text {
  color: #9a6700;
}
.ok-text {
  color: #248a3d;
}
.notice {
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.05);
  margin: 12px 0;
}
.notice.warn {
  background: rgba(255, 159, 10, 0.18);
}
.errlist {
  padding-left: 18px;
}
@media (prefers-color-scheme: dark) {
  .page {
    background: #000;
    color: #f5f5f7;
  }
  .toolbar {
    background: rgba(20, 20, 22, 0.6);
    border-bottom-color: rgba(255, 255, 255, 0.12);
  }
  .panel {
    background: rgba(28, 28, 30, 0.8);
    border-color: rgba(255, 255, 255, 0.1);
  }
  input[type='text'],
  input[type='number'],
  textarea,
  select {
    background: #1c1c1e;
    border-color: rgba(255, 255, 255, 0.2);
    color: #f5f5f7;
  }
  .ghost {
    border-color: rgba(255, 255, 255, 0.25);
  }
  .hint {
    color: #a1a1a6;
  }
  .notice {
    background: rgba(255, 255, 255, 0.08);
  }
}
@media (prefers-reduced-motion: reduce) {
  .page {
    opacity: 1;
    transform: none;
  }
  .page.entered,
  .primary,
  .ghost,
  input,
  textarea,
  select {
    transition: none;
  }
}
</style>
