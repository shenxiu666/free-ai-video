import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  fetchKeysList,
  addKeyRequest,
  fetchModelsConfig,
  saveModelsConfig,
  fetchPersistStatus,
  activatePool,
  loadFakeKeys,
  persistFakeKeys,
  loadLocalModels,
  persistLocalModels,
  computeMask,
  defaultModels,
  type BackendStatus,
  type KeyItem,
  type ModelsConfig,
  type PersistStatus
} from '../api/keys'
import {
  testTextConnectivity,
  fetchTextStatus,
  saveCustomKey,
  type TextTestOverrides,
  type TextTestResult,
  type TextStatus
} from '../api/text'
import {
  fetchFreeModels,
  type FreeModel
} from '../api/opencode'

export type { BackendStatus, KeyItem, ModelsConfig }

export const useKeysStore = defineStore('keys', () => {
  const keys = ref<KeyItem[]>([])
  const backendStatus = ref<BackendStatus>('未加密')
  const models = ref<ModelsConfig>(defaultModels())
  const loading = ref(false)
  const freeModels = ref<FreeModel[]>([])
  const freeModelsSource = ref('')
  const freeModelsNote = ref('')
  const pullingModels = ref(false)
  const textStatus = ref<TextStatus | null>(null)
  const persist = ref<PersistStatus | null>(null)
  const activating = ref(false)

  /** 拉取密钥池（掩码 only）。失败则读 localStorage 假 keys，状态徽保持上次已知值。 */
  async function fetchKeys(): Promise<void> {
    loading.value = true
    try {
      const res = await fetchKeysList()
      keys.value = res.keys
      backendStatus.value = res.backendStatus
    } catch {
      keys.value = loadFakeKeys()
    } finally {
      loading.value = false
    }
  }

  /**
   * 新增 Key。raw 明文只进本次 POST body；无论成功失败，
   * 本地副本在 finally 中清零，且从不写入 state / localStorage / 日志。
   * 调用方（Keys.vue）同样负责清空输入框。
   */
  async function addKey(rawKey: string, poolType: string, accountTag: string): Promise<void> {
    let payload: { raw_key: string; pool_type: string; account_tag: string } | null = {
      raw_key: rawKey,
      pool_type: poolType,
      account_tag: accountTag
    }
    try {
      await addKeyRequest(payload)
      await fetchKeys().catch(() => {
        /* 刷新失败则保留当前列表 */
      })
    } catch {
      // 离线兜底：本地追加一条掩码记录（掩码先算好，raw 不留存）
      const entry: KeyItem = {
        id: `local-${Date.now()}`,
        mask: computeMask(rawKey),
        status: 2,
        pool_type: poolType || 'free',
        usage: { text_n: 0, img_n: 0, video_s: 0 },
        limits: {},
        account_tag: accountTag
      }
      keys.value = [...keys.value, entry]
      persistFakeKeys(keys.value)
    } finally {
      if (payload) {
        payload.raw_key = ''
        payload.pool_type = ''
        payload.account_tag = ''
        payload = null
      }
    }
  }

  async function fetchModels(): Promise<void> {
    try {
      const m = await fetchModelsConfig()
      models.value = m ?? loadLocalModels()
    } catch {
      models.value = loadLocalModels()
    }
  }

  /** 模型参数本地必存、远端尽力存（离线不报错）。 */
  async function saveModels(): Promise<void> {
    persistLocalModels(models.value)
    try {
      await saveModelsConfig(models.value)
    } catch {
      /* 离线：仅本地保存 */
    }
  }

  /** 拉取 opencode 免费模型：live 失败后端自动回内置候选，照单收下并记录来源。 */
  async function pullFreeModels(refresh = false): Promise<void> {
    pullingModels.value = true
    try {
      const res = await fetchFreeModels('opencode', refresh)
      freeModels.value = res.models
      freeModelsSource.value = res.source
      freeModelsNote.value = res.note || ''
    } finally {
      pullingModels.value = false
    }
  }

  /** 文本通道状态（密钥来源 + 自建密钥是否已配），失败保留上次值。 */
  async function loadTextStatus(): Promise<void> {
    try {
      textStatus.value = await fetchTextStatus()
    } catch {
      /* 离线：保留上次值 */
    }
  }

  /** 录入自建端点专用密钥（进加密池）；明文只进本次请求，调后即清。 */
  async function saveEndpointKey(rawKey: string): Promise<string> {
    let payload: { raw_key: string } | null = { raw_key: rawKey }
    try {
      const res = await saveCustomKey(payload.raw_key)
      await loadTextStatus().catch(() => {
        /* 刷新失败保留当前 */
      })
      return res.mask
    } finally {
      if (payload) {
        payload.raw_key = ''
        payload = null
      }
    }
  }

  /** 连通性测试：用当前表单值（未保存也可测）真实 ping 一次文本端点。 */
  async function testText(overrides?: TextTestOverrides): Promise<TextTestResult> {
    const t = models.value.text
    return testTextConnectivity({
      provider: t.provider,
      model: t.model,
      base_url: t.base_url,
      temperature: t.temperature,
      max_tokens: t.max_tokens,
      timeout_s: t.timeout_s,
      ...overrides
    })
  }

  /** 持久化状态（激活/写透/DB），失败保留上次值。 */
  async function loadPersist(): Promise<void> {
    try {
      persist.value = await fetchPersistStatus()
    } catch {
      /* 离线：保留上次值 */
    }
  }

  /** 一键激活；成功后刷新状态与列表。 */
  async function activate(force = false): Promise<PersistStatus> {
    activating.value = true
    try {
      persist.value = await activatePool(force)
      await fetchKeys().catch(() => {
        /* 刷新失败保留当前列表 */
      })
      return persist.value
    } finally {
      activating.value = false
    }
  }

  return { keys, backendStatus, models, loading, fetchKeys, addKey, fetchModels, saveModels, testText, freeModels, freeModelsSource, freeModelsNote, pullingModels, pullFreeModels, textStatus, loadTextStatus, saveEndpointKey, persist, activating, loadPersist, activate }
})
