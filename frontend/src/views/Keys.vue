<template>
  <section class="keys" aria-label="密钥池与设置">
    <header class="k-head">
      <div>
        <h1 class="k-title">密钥池 · 设置</h1>
        <p class="k-sub">多密钥管理 · 文本/语音/图片/视频模型参数（前端仅见掩码）</p>
      </div>
      <span class="sec" :class="secCls" role="status" :title="secTitle">
        <i class="sec-dot" aria-hidden="true" />
        {{ keys.backendStatus }}
      </span>
    </header>

    <div v-if="showPersistWarn" class="warn">
      {{ persistWarnText }}
      <button type="button" class="btn sm" :disabled="keys.activating" @click="onActivate(false)">
        {{ keys.activating ? '激活中…' : '一键激活' }}
      </button>
    </div>

    <div class="card">
      <div class="sec-bar">
        <strong>持久化 · {{ persistStateText }}</strong>
        <button type="button" class="btn ghost sm" :disabled="keys.activating" @click="onActivate(false)">
          {{ keys.activating ? '激活中…' : '一键激活' }}
        </button>
      </div>
      <p class="hint">{{ persistDetailText }}</p>
      <p v-if="persistMsg" class="hint" :class="{ ok: persistOk }">{{ persistMsg }}</p>
    </div>

    <div class="card">
      <div class="sec-bar">
        <strong>密钥（{{ keys.keys.length }}）</strong>
        <button type="button" class="btn ghost sm" :disabled="keys.loading" @click="onRefresh">
          {{ keys.loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <p v-if="!keys.keys.length" class="empty">暂无密钥：后端恢复后自动拉取；离线时可用下方表单新增（仅掩码落盘）。</p>
      <ul class="klist">
        <li v-for="k in keys.keys" :key="k.id" class="kitem">
          <div class="krow">
            <code class="mask">{{ k.mask }}</code>
            <span class="badge" :class="badge(k).cls">{{ badge(k).label }}</span>
            <span class="pool">{{ poolLabel(k.pool_type) }}</span>
          </div>
          <div class="usage">
            <span class="u-text">文本 {{ k.usage?.text_n ?? 0 }} · 图片 {{ k.usage?.img_n ?? 0 }} · 视频 {{ k.usage?.video_s ?? 0 }}s</span>
            <span v-if="k.limits?.rpm != null" class="u-text">每分钟限速 {{ k.limits?.rpm }}</span>
          </div>
          <div v-if="quotaPct(k) !== null" class="quota" role="progressbar" :aria-valuenow="quotaPct(k) ?? 0" aria-valuemin="0" aria-valuemax="100" aria-label="日配额占用">
            <div class="qfill" :style="{ width: (quotaPct(k) ?? 0) + '%' }" />
          </div>
          <div class="krow foot">
            <span class="acct">账号备注 {{ maskAccountTag(k.account_tag) }}</span>
            <span v-if="coolLeft(k)" class="cool">冷却 {{ coolLeft(k) }}</span>
            <span v-else-if="k.limits?.day_quota != null" class="acct">日配额 {{ k.limits?.day_quota }}</span>
          </div>
        </li>
      </ul>
    </div>

    <div class="card">
      <h2 class="h2">新增密钥</h2>
      <form @submit.prevent="onAdd">
        <label class="field">
          <span>密钥明文（密码框，永不回显，提交后清空）</span>
          <input
            ref="rawInput"
            v-model="rawKey"
            type="password"
            autocomplete="new-password"
            placeholder="sk-…"
          />
        </label>
        <div class="grid2">
          <label class="field">
            <span>池类型</span>
            <select v-model="poolType">
              <option value="free">免费（默认·独立池）</option>
              <option value="enterprise">企业版</option>
              <option value="tokenplan">套餐版</option>
            </select>
          </label>
          <label class="field">
            <span>账号备注（不同账号=独立池）</span>
            <input v-model="accountTag" type="text" autocomplete="off" placeholder="如 acc-01" />
          </label>
        </div>
        <div class="btn-row">
          <button type="button" class="btn primary" :disabled="!rawKey" @click="onAdd">提交</button>
        </div>
      </form>
      <p v-if="addMsg" class="hint ok">{{ addMsg }}</p>
      <p class="hint">安全：提交后输入框立即清空；抓包/日志/存储中只出现 sk-前2~~~~后3 掩码。</p>
    </div>

    <div class="card">
      <h2 class="h2">opencode 调用（开发期 Agent，运行时不强依赖）</h2>
      <p class="hint">默认使用内置二进制（<code>tools/opencode/opencode.exe</code>，锁定版本 {{ oc.status?.pinned_version || '未知' }}）。缺失不影响出片；Zen 登录等需交互命令请用选中的二进制在终端执行。</p>
      <div class="sec-bar">
        <span class="u-text">当前生效：{{ ocEffectiveText }}</span>
        <span v-if="ocVersionBadge" class="pool">{{ ocVersionBadge }}</span>
      </div>
      <fieldset>
        <legend>二进制选择</legend>
        <div class="radios">
          <label><input v-model="ocMode" type="radio" value="builtin" /> 内置（默认）</label>
          <label><input v-model="ocMode" type="radio" value="system" /> 系统 PATH</label>
          <label><input v-model="ocMode" type="radio" value="custom" /> 自定义路径</label>
          <label><input v-model="ocMode" type="radio" value="auto" /> 自动（内置→PATH）</label>
        </div>
        <label v-if="ocMode === 'custom'" class="field">
          <span>二进制路径（opencode 可执行文件绝对路径）</span>
          <input v-model="ocBinPath" type="text" autocomplete="off" placeholder="如 C:\tools\opencode\opencode.exe" />
        </label>
        <div class="btn-row">
          <button type="button" class="btn primary" :disabled="oc.saving" @click="onOcSave">
            {{ oc.saving ? '保存中…' : '保存选择' }}
          </button>
          <button type="button" class="btn" :disabled="oc.detecting" @click="onOcDetect">
            {{ oc.detecting ? '检测中…' : '检测全部位置' }}
          </button>
          <button type="button" class="btn ghost" :disabled="oc.testing" @click="onOcTest">
            {{ oc.testing ? '测试中…' : '测试可用性' }}
          </button>
        </div>
      </fieldset>
      <ul v-if="oc.status?.candidates?.length" class="klist">
        <li v-for="c in oc.status.candidates" :key="c.source + c.path" class="kitem">
          <div class="krow">
            <code class="mask">{{ ocSourceLabel(c.source) }}</code>
            <span class="badge" :class="c.exists ? 'b-idle' : 'b-empty'">{{ c.exists ? '存在' : '缺失' }}</span>
            <span v-if="c.version" class="pool">v{{ c.version }}</span>
          </div>
          <div class="usage"><span class="u-text">{{ c.path }}</span></div>
          <div v-if="c.error" class="usage"><span class="u-text">⚠ {{ c.error }}</span></div>
        </li>
      </ul>
      <p v-if="oc.message" class="hint" :class="{ ok: oc.lastTest?.ok }">{{ oc.message }}</p>
      <p v-if="oc.lastTest && !oc.lastTest.ok" class="warn">内置缺失时跑 <code>.\tools\opencode\install.ps1</code> 按锁定版本配给。</p>
    </div>

    <div class="card">
      <h2 class="h2">模型参数（对应 config/models.yaml）</h2>
      <p v-if="!keys.models.text.provider && !keys.models.text.model" class="warn">
        文本模型留空：新建任务将被拒绝，请先填写提供商 / 模型（降级策略默认手动，不自动降级）。
      </p>
      <fieldset>
        <legend>文本（无主力·用户自配，可留空）</legend>
        <div class="grid2">
          <label class="field"><span>提供商</span>
            <input v-model="keys.models.text.provider" type="text" autocomplete="off" list="text-provider-list" placeholder="留空=拒绝任务" />
            <datalist id="text-provider-list">
              <option value="agnes">Agnes</option>
              <option value="opencode-zen">opencode Zen（免费）</option>
              <option value="openai-compatible">OpenAI 兼容（自建/第三方）</option>
            </datalist>
          </label>
          <label class="field"><span>模型</span><input v-model="keys.models.text.model" type="text" autocomplete="off" list="text-model-list" placeholder="留空=拒绝任务，如 agnes-3.0-flash" /></label>
          <datalist id="text-model-list">
            <option v-for="m in keys.freeModels" :key="m.ref" :value="m.model">{{ m.name || m.ref }}</option>
          </datalist>
        </div>
        <div class="btn-row">
          <button type="button" class="btn" :disabled="keys.pullingModels" @click="onPullModels">
            {{ keys.pullingModels ? '拉取中…' : '拉取免费模型' }}
          </button>
        </div>
        <label v-if="keys.freeModels.length" class="field"><span>免费模型选择（{{ keys.freeModelsSource === 'live' ? '实时拉取' : '内置候选' }}，选中自动填入上两栏）</span>
          <select v-model="pickedFreeModel" @change="onPickFreeModel">
            <option value="">请选择…</option>
            <option v-for="m in keys.freeModels" :key="m.ref" :value="m.ref">{{ m.name || m.model }}（{{ m.ref }}）{{ m.free ? '·免费' : '' }}</option>
          </select>
        </label>
        <p v-if="keys.freeModelsNote" class="hint">{{ keys.freeModelsNote }}</p>
        <p v-if="isZenPicked" class="hint ok">opencode 免费模型免登录，走本地 opencode CLI 通道（输出只取回答正文，采样参数用模型默认）。</p>
        <p class="hint">密钥来源：{{ textKeySourceHint }}</p>
        <div v-if="isCustomProvider" class="subbox">
          <label class="field"><span>端点密钥（专用，进加密池，不存配置文件）</span>
            <input
              ref="customKeyInput"
              v-model="customKey"
              type="password"
              autocomplete="new-password"
              placeholder="自建端点的 Bearer Key"
            />
          </label>
          <div class="btn-row">
            <button type="button" class="btn" :disabled="!customKey || savingCustomKey" @click="onSaveCustomKey">
              {{ savingCustomKey ? '保存中…' : '保存端点密钥' }}
            </button>
            <span class="u-text">{{ customKeyStateText }}</span>
          </div>
          <p v-if="customKeyMsg" class="hint" :class="{ ok: customKeyOk }">{{ customKeyMsg }}</p>
        </div>
        <div class="grid2">
          <label class="field"><span>接口协议</span>
            <select v-model="keys.models.text.protocol">
              <option value="openai">OpenAI 兼容（Agnes/Zen/自建均用此）</option>
            </select>
          </label>
          <label class="field"><span>接口端点</span><input v-model="keys.models.text.base_url" type="text" autocomplete="off" placeholder="留空用提供商默认；自建如 http://127.0.0.1:11434/v1" /></label>
        </div>
        <div class="grid2">
          <label class="field"><span>采样温度（0-2）</span><input v-model.number="keys.models.text.temperature" type="number" min="0" max="2" step="0.1" /></label>
          <label class="field"><span>最大 Token 数</span><input v-model.number="keys.models.text.max_tokens" type="number" min="512" step="512" /></label>
        </div>
        <div class="grid2">
          <label class="field"><span>超时（秒）</span><input v-model.number="keys.models.text.timeout_s" type="number" min="10" step="5" /></label>
          <label class="field"><span>降级策略（默认手动，仅显式配置才降级）</span>
            <select v-model="keys.models.text.fallback">
              <option value="manual">手动</option>
              <option value="auto">自动</option>
            </select>
          </label>
        </div>
        <div class="btn-row">
          <button type="button" class="btn" :disabled="textTesting" @click="onTestText">
            {{ textTesting ? '测试中…' : '测试连通性' }}
          </button>
        </div>
        <p v-if="textTestMsg" class="hint" :class="{ ok: textTestOk }">{{ textTestMsg }}</p>
        <p class="hint">密钥走上方密钥池取用归还，测试只发一条极短消息并计时（计一次文本调用）。</p>
      </fieldset>
      <fieldset>
        <legend>语音合成（默认 edge-tts，失败手动切换）</legend>
        <div class="grid2">
          <label class="field"><span>提供商</span>
            <select v-model="keys.models.tts.provider">
              <option value="edge-tts">edge-tts</option>
              <option value="Kokoro-82M">Kokoro-82M</option>
              <option value="CosyVoice3">CosyVoice3</option>
              <option value="Qwen3-TTS">Qwen3-TTS</option>
              <option value="IndexTTS2">IndexTTS2</option>
            </select>
          </label>
          <label class="field"><span>音色</span><input v-model="keys.models.tts.voice" type="text" autocomplete="off" /></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>图片（agnes-image-2.5-flash）</legend>
        <div class="grid2">
          <label class="field"><span>模型（锁定）</span><input v-model="keys.models.image.model" type="text" readonly /></label>
          <label class="field"><span>清晰度（仅 1K/2K/3K/4K）</span>
            <select v-model="keys.models.image.size">
              <option value="1K">1K</option>
              <option value="2K">2K</option>
              <option value="3K">3K</option>
              <option value="4K">4K</option>
            </select>
          </label>
        </div>
        <label class="field"><span>画幅比例</span>
          <select v-model="keys.models.image.ratio">
            <option value="9:16">9:16（竖屏默认）</option>
            <option value="16:9">16:9</option>
            <option value="1:1">1:1</option>
          </select>
        </label>
      </fieldset>
      <fieldset>
        <legend>视频（agnes-video-2.5-flash）</legend>
        <div class="grid2">
          <label class="field"><span>模型（锁定）</span><input v-model="keys.models.video.model" type="text" readonly /></label>
          <label class="field"><span>清晰度（锁定 720P）</span><input v-model="keys.models.video.size" type="text" readonly /></label>
        </div>
        <div class="grid2">
          <label class="field"><span>单镜时长（秒）</span>
            <select v-model="keys.models.video.seconds">
              <option v-for="s in ['4', '5', '6', '7', '8', '9', '10', '11', '12']" :key="s" :value="s">{{ s }}s</option>
            </select>
          </label>
          <label class="field"><span>生成模式</span>
            <select v-model="keys.models.video.mode">
              <option value="text">纯文本</option>
              <option value="keyframe">首尾帧</option>
              <option value="reference">参考图</option>
            </select>
          </label>
        </div>
      </fieldset>
      <div class="btn-row">
        <button type="button" class="btn primary" @click="onSaveModels">保存模型参数</button>
      </div>
      <p v-if="saveMsg" class="hint ok">{{ saveMsg }}</p>
      <p class="hint">产物将标注来源（图片/视频走 Agnes 免费模型，文本按所选模型）。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useKeysStore } from '../stores/keys'
import { useOpencodeStore, type OpencodeMode } from '../stores/opencode'
import { maskAccountTag, type KeyItem } from '../api/keys'

const keys = useKeysStore()
const oc = useOpencodeStore()
const ocMode = ref<OpencodeMode>('builtin')
const ocBinPath = ref('')
const rawKey = ref('')
const poolType = ref('free')
const accountTag = ref('')
const addMsg = ref('')
const saveMsg = ref('')
const rawInput = ref<HTMLInputElement | null>(null)
const now = ref(Date.now())
let timer: number | null = null

onMounted(async () => {
  await keys.fetchKeys()
  await keys.fetchModels()
  await keys.loadTextStatus()
  await keys.loadPersist()
  await oc.load()
  if (oc.status) {
    const m = oc.status.selection.mode
    if (m === 'builtin' || m === 'system' || m === 'custom' || m === 'auto') ocMode.value = m
    ocBinPath.value = oc.status.selection.bin_path || ''
  }
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer)
  timer = null
})

const secCls = computed(() => {
  if (keys.backendStatus === 'TPM锁定') return 'ok'
  if (keys.backendStatus === 'DPAPI兜底') return 'warn'
  return 'plain'
})

const secTitle = computed(() => {
  if (keys.backendStatus === 'TPM锁定') return 'TPM 密封机器码 + AES-256-GCM 落盘'
  if (keys.backendStatus === 'DPAPI兜底') return '无 TPM，DPAPI CurrentUser 兜底'
  return '未加密：仅允许开发调试，正式版禁止写入明文 Key'
})

function isCooling(k: KeyItem): boolean {
  if (k.cooling) return true
  if (k.cooldown_until) {
    const t = Date.parse(k.cooldown_until)
    if (Number.isFinite(t)) return t > now.value
  }
  return false
}

/** 池类型英文标识 → 中文名（标识本身是 API 约定，保持原值传输） */
function poolLabel(poolType: string): string {
  if (poolType === 'enterprise') return '企业版'
  if (poolType === 'tokenplan') return '套餐版'
  return '免费'
}

/** opencode 候选来源英文标识 → 中文名 */
function ocSourceLabel(source: string): string {
  if (source === 'builtin') return '内置'
  if (source === 'env') return '环境变量'
  if (source === 'path') return '系统 PATH'
  if (source === 'npm-global') return 'npm 全局'
  if (source === 'user') return '用户目录'
  if (source === 'custom') return '自定义'
  return source
}

function badge(k: KeyItem): { label: string; cls: string } {
  if (k.revoked) return { label: '作废', cls: 'b-revoked' }
  if (isCooling(k)) return { label: '冷却中', cls: 'b-cooling' }
  if (k.status === 2) return { label: '空闲', cls: 'b-idle' }
  if (k.status === 1) return { label: '使用中', cls: 'b-busy' }
  if (k.status === 0) return { label: '用光', cls: 'b-empty' }
  return { label: '未知', cls: 'b-unknown' }
}

/** 日配额占用：分组 limits {text/image/video:{day_quota}} 取最大配额为分母，
 * 分子取三类用量最大值占比（次/张/秒单位不一致，不混算求和）。无配额返回 null。 */
function quotaPct(k: KeyItem): number | null {
  const lim = (k.limits ?? {}) as Record<string, unknown>
  const quotas: number[] = []
  for (const v of Object.values(lim)) {
    if (v && typeof v === 'object') {
      const q = (v as Record<string, unknown>)['day_quota']
      if (typeof q === 'number' && Number.isFinite(q) && q > 0) quotas.push(q)
    }
  }
  const flatQuota = Number((lim as Record<string, unknown>)['day_quota'] ?? 0)
  if (quotas.length === 0 && (!Number.isFinite(flatQuota) || flatQuota <= 0)) return null
  const denom = quotas.length ? Math.max(...quotas) : flatQuota
  const used = Math.max(
    Number(k.usage?.text_n ?? 0),
    Number(k.usage?.img_n ?? 0),
    Number(k.usage?.video_s ?? 0)
  )
  return Math.min(100, Math.round((used / denom) * 100))
}

function coolLeft(k: KeyItem): string | null {
  if (!k.cooldown_until) return k.cooling ? '冷却中' : null
  const t = Date.parse(k.cooldown_until)
  if (!Number.isFinite(t)) return k.cooling ? '冷却中' : null
  const diff = t - now.value
  if (diff <= 0) return null
  const s = Math.ceil(diff / 1000)
  const mm = Math.floor(s / 60)
  const ss = s % 60
  if (mm >= 60) return `${Math.floor(mm / 60)}h${mm % 60}m`
  return `${mm}:${String(ss).padStart(2, '0')}`
}

async function onRefresh(): Promise<void> {
  await keys.fetchKeys()
}

async function onAdd(): Promise<void> {
  if (!rawKey.value) return
  await keys.addKey(rawKey.value, poolType.value, accountTag.value)
  // 前端内存用后即清：输入框立即清空并失焦
  rawKey.value = ''
  try {
    rawInput.value?.blur()
  } catch {
    /* 忽略 */
  }
  addMsg.value = '已提交（明文已清空，列表仅显示掩码）'
  window.setTimeout(() => {
    addMsg.value = ''
  }, 3000)
}

async function onSaveModels(): Promise<void> {
  await keys.saveModels()
  saveMsg.value = '已保存（离线时仅存本地）'
  window.setTimeout(() => {
    saveMsg.value = ''
  }, 3000)
}

const textTesting = ref(false)
const textTestMsg = ref('')
const textTestOk = ref(false)
const pickedFreeModel = ref('')
const customKey = ref('')
const customKeyInput = ref<HTMLInputElement | null>(null)
const savingCustomKey = ref(false)
const customKeyMsg = ref('')
const customKeyOk = ref(false)
const persistMsg = ref('')
const persistOk = ref(false)

/** 未激活/需重激活/内存态任一命中即顶栏告警。 */
const showPersistWarn = computed(() => {
  const p = keys.persist
  if (!p) return false
  return !p.persistent || p.needs_reactivation || !p.activated
})

const persistWarnText = computed(() => {
  const p = keys.persist
  if (p?.needs_reactivation) return '密钥库解封失败（疑似换机/重装）：读不受影响，写已锁定，点一键激活可强制重签（旧库解不开会自动归档）。'
  if (p && !p.activated) return '未激活：密钥只在内存，重启后端即丢失，且之前录的已丢失需重录一次。点一键激活后不再丢失。'
  return '内存模式：密钥未写透加密盘，重启后端即丢失，请点一键激活。'
})

const persistStateText = computed(() => {
  const p = keys.persist
  if (!p) return '未知'
  if (p.needs_reactivation) return '需重新激活'
  if (p.persistent) return `已激活·持久化（${p.backend_status}）`
  if (p.activated) return `${p.backend_status}（内存态）`
  return '未激活'
})

const persistDetailText = computed(() => {
  const p = keys.persist
  if (!p) return '正在读取持久化状态…'
  const parts = [`内存 ${p.memory_keys} 条`, p.db_exists ? '加密盘有库' : '加密盘无库']
  if (p.db_path) parts.push(p.db_path)
  return parts.join(' · ')
})

async function onActivate(force: boolean): Promise<void> {
  persistMsg.value = ''
  persistOk.value = false
  try {
    const st = await keys.activate(force)
    persistOk.value = true
    const bits = [`已激活（${st.backend_status}）`]
    if (typeof st.migrated === 'number' && st.migrated > 0) bits.push(`迁入内存密钥 ${st.migrated} 条`)
    if (st.archived) bits.push(`旧库已归档：${st.archived}`)
    if (st.already) bits.push('（原本就可用，直接恢复）')
    persistMsg.value = bits.join('，') + '。之前未激活时录的密钥已随重启丢失，需重新录入一次，之后不再丢失。'
  } catch (e) {
    persistMsg.value = `激活失败：${e instanceof Error ? e.message : String(e)}`
  }
}

async function onSaveCustomKey(): Promise<void> {
  if (!customKey.value) return
  savingCustomKey.value = true
  customKeyMsg.value = ''
  customKeyOk.value = false
  try {
    const mask = await keys.saveEndpointKey(customKey.value)
    customKeyOk.value = true
    customKeyMsg.value = `已存入加密池（${mask}），配置文件无残留`
  } catch (e) {
    customKeyMsg.value = `保存失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    customKey.value = ''
    try {
      customKeyInput.value?.blur()
    } catch {
      /* 忽略 */
    }
    savingCustomKey.value = false
  }
}

const isZenPicked = computed(() => keys.models.text.provider === 'opencode-zen')
const isCustomProvider = computed(() => keys.models.text.provider === 'openai-compatible')

const textKeySourceHint = computed(() => {
  const p = keys.models.text.provider
  if (p === 'opencode-zen') return '本地 CLI（免登录，不碰密钥池）'
  if (p === 'openai-compatible') return '下方端点密钥（专用，与 Agnes 密钥绝不混用）'
  if (p === 'agnes') return '上方密钥池（仅 Agnes Key，自建密钥不混用）'
  return '未选择提供商'
})

const customKeyStateText = computed(() => {
  const c = keys.textStatus?.custom_key
  if (!c) return '专用密钥：未知（点测试或保存后刷新）'
  return c.configured ? `专用密钥：已配置（${c.mask ?? ''}）` : '专用密钥：未配置'
})

async function onPullModels(): Promise<void> {
  pickedFreeModel.value = ''
  try {
    await keys.pullFreeModels()
  } catch (e) {
    textTestOk.value = false
    textTestMsg.value = `拉取失败：${e instanceof Error ? e.message : String(e)}`
  }
}

/** 选中免费模型 → 自动填提供商/模型（opencode 系走免登录 CLI 通道，端点留空）。 */
function onPickFreeModel(): void {
  const found = keys.freeModels.find((m) => m.ref === pickedFreeModel.value)
  if (!found) return
  if (found.provider === 'opencode') {
    keys.models.text.provider = 'opencode-zen'
    keys.models.text.model = found.model
    keys.models.text.base_url = ''
  } else {
    keys.models.text.provider = found.provider
    keys.models.text.model = found.model
    if (found.url) keys.models.text.base_url = found.url
  }
}

/** 连通性测试：用当前表单值真实 ping 一次文本端点（未保存也可测）。 */
async function onTestText(): Promise<void> {
  textTesting.value = true
  textTestMsg.value = ''
  textTestOk.value = false
  try {
    const r = await keys.testText()
    textTestOk.value = true
    textTestMsg.value = `连通成功：${r.provider} / ${r.model}，延迟 ${r.latency_ms}ms，模型回复“${r.reply || '（空）'}”`
  } catch (e) {
    textTestMsg.value = `连通失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    textTesting.value = false
  }
}

/** opencode 当前生效项一行描述 */
const ocEffectiveText = computed(() => {
  const e = oc.status?.effective
  if (!e || !e.path) return '无可用二进制（内置缺失且 PATH 无 opencode）'
  return `${e.path}${e.version ? ` · v${e.version}` : ''}${e.exists ? '' : '（文件缺失）'}`
})

/** 版本徽：一致 / 不一致 / 未知 */
const ocVersionBadge = computed(() => {
  const e = oc.status?.effective
  if (!e?.version || !e.pinned) return ''
  if (e.pinned_match === true) return '与锁定一致'
  if (e.pinned_match === false) return `与锁定 ${e.pinned} 不一致`
  return ''
})

async function onOcSave(): Promise<void> {
  const ok = await oc.save(ocMode.value, ocBinPath.value.trim())
  if (ok && oc.status) {
    ocBinPath.value = oc.status.selection.bin_path || ''
  }
}

async function onOcDetect(): Promise<void> {
  await oc.detect()
}

async function onOcTest(): Promise<void> {
  await oc.test(ocMode.value === 'custom' ? ocBinPath.value.trim() : '')
}
</script>

<style scoped>
.keys {
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

.k-head {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
}

.k-title {
  margin: 0;
  font-size: clamp(1.4rem, 1.1rem + 2vw, 2rem);
  line-height: 1.1;
  letter-spacing: -0.02em;
  font-weight: 700;
}

.k-sub {
  margin: 0.25rem 0 0;
  font-size: 0.85rem;
  color: var(--sub);
}

.sec {
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

.sec-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--gray);
}

.sec.ok .sec-dot {
  background: var(--green);
}

.sec.warn .sec-dot {
  background: var(--amber);
}

.card {
  background: var(--card);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  backdrop-filter: blur(20px) saturate(180%);
  border: 1px solid var(--edge);
  border-radius: 16px;
  padding: 0.9rem;
}

.sec-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
  font-size: 0.9rem;
}

.h2 {
  margin: 0 0 0.6rem;
  font-size: 1rem;
  letter-spacing: -0.01em;
}

.empty {
  font-size: 0.82rem;
  color: var(--sub);
}

.klist {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.kitem {
  border: 1px solid var(--edge);
  border-radius: 12px;
  padding: 0.65rem 0.75rem;
  background: rgba(255, 255, 255, 0.6);
}

.krow {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.krow.foot {
  margin-top: 0.35rem;
}

.mask {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.82rem;
  font-weight: 700;
}

.badge {
  font-size: 0.72rem;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  color: #fff;
  transition: transform 0.4s var(--spring);
}

.b-idle {
  background: var(--green);
}

.b-busy {
  background: var(--blue);
}

.b-empty {
  background: var(--gray);
}

.b-cooling {
  background: var(--amber);
}

.b-revoked {
  background: var(--red);
}

.b-unknown {
  background: var(--gray);
  opacity: 0.7;
}

.pool {
  font-size: 0.72rem;
  color: var(--sub);
  border: 1px solid var(--edge);
  border-radius: 999px;
  padding: 0.12rem 0.55rem;
}

.usage {
  display: flex;
  gap: 0.8rem;
  flex-wrap: wrap;
  margin-top: 0.4rem;
}

.u-text {
  font-size: 0.78rem;
  color: var(--sub);
  font-variant-numeric: tabular-nums;
}

.quota {
  height: 8px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.08);
  overflow: hidden;
  margin-top: 0.4rem;
}

.qfill {
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--green), var(--amber));
  transition: width 0.55s var(--spring);
}

.acct,
.cool {
  font-size: 0.76rem;
  color: var(--sub);
}

.cool {
  color: var(--amber);
  font-variant-numeric: tabular-nums;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.8rem;
  color: var(--sub);
  margin-bottom: 0.6rem;
}

.field input,
.field select {
  font: inherit;
  color: var(--ink);
  padding: 0.55rem 0.7rem;
  border-radius: 10px;
  border: 1px solid var(--edge);
  background: rgba(255, 255, 255, 0.9);
}

.field input[readonly] {
  opacity: 0.65;
}

.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 0.7rem;
}

@media (max-width: 560px) {
  .grid2 {
    grid-template-columns: 1fr;
  }
}

fieldset {
  border: 1px solid var(--edge);
  border-radius: 12px;
  margin: 0 0 0.7rem;
  padding: 0.7rem 0.8rem 0.2rem;
}

legend {
  font-size: 0.82rem;
  font-weight: 700;
  padding: 0 0.4rem;
}

.btn-row {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-top: 0.2rem;
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
  transition: transform 100ms ease, opacity 100ms ease;
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

.btn.sm {
  padding: 0.3rem 0.7rem;
  font-size: 0.78rem;
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.hint {
  font-size: 0.78rem;
  color: var(--sub);
  margin: 0.5rem 0 0;
}

.hint.ok {
  color: var(--green);
}

.warn {
  font-size: 0.82rem;
  color: var(--amber);
  background: rgba(255, 159, 10, 0.12);
  border: 1px solid rgba(255, 159, 10, 0.4);
  border-radius: 10px;
  padding: 0.5rem 0.7rem;
}

.radios {
  display: flex;
  gap: 0.9rem;
  flex-wrap: wrap;
  font-size: 0.85rem;
  margin-bottom: 0.6rem;
}

.radios label {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  cursor: pointer;
}

.subbox {
  border: 1px dashed var(--edge);
  border-radius: 12px;
  padding: 0.7rem 0.8rem 0.2rem;
  margin: 0 0 0.7rem;
}

.subbox .u-text {
  align-self: center;
}

code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.78em;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 6px;
  padding: 0.05rem 0.35rem;
}

@media (prefers-reduced-motion: reduce) {
  .badge,
  .qfill,
  .btn {
    transition-duration: 0.01ms;
  }
  .btn:active {
    transform: none;
  }
}

@media (prefers-color-scheme: dark) {
  .keys {
    --ink: #f5f5f7;
    --sub: rgba(245, 245, 247, 0.65);
    --card: rgba(28, 28, 30, 0.72);
    --edge: rgba(255, 255, 255, 0.14);
  }
  .kitem,
  .field input,
  .field select,
  .btn {
    background: rgba(28, 28, 30, 0.9);
    color: var(--ink);
  }
  code {
    background: rgba(255, 255, 255, 0.12);
  }
}

.field input:focus-visible,
.field select:focus-visible,
.btn:focus-visible {
  outline: 2px solid #0a84ff;
  outline-offset: 2px;
}
</style>
