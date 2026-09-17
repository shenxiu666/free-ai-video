import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  fetchOpencodeStatus,
  detectOpencode,
  selectOpencode,
  testOpencode,
  loadLocalOpencodeStatus,
  persistLocalOpencodeStatus,
  type OpencodeMode,
  type OpencodeStatus,
  type OpencodeTestResult
} from '../api/opencode'

export type { OpencodeMode, OpencodeStatus, OpencodeTestResult }

export const useOpencodeStore = defineStore('opencode', () => {
  const status = ref<OpencodeStatus | null>(loadLocalOpencodeStatus())
  const loading = ref(false)
  const detecting = ref(false)
  const saving = ref(false)
  const testing = ref(false)
  const message = ref('')
  const lastTest = ref<OpencodeTestResult | null>(null)

  function applyStatus(s: OpencodeStatus): void {
    status.value = s
    persistLocalOpencodeStatus(s)
  }

  async function load(): Promise<void> {
    loading.value = true
    try {
      applyStatus(await fetchOpencodeStatus())
      message.value = ''
    } catch (e) {
      message.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** 全量版本探测：刷新候选列表的 version/error（较慢，点按触发）。 */
  async function detect(): Promise<void> {
    detecting.value = true
    try {
      const res = await detectOpencode()
      if (status.value) {
        applyStatus({ ...status.value, candidates: res.candidates })
      }
      message.value = ''
    } catch (e) {
      message.value = e instanceof Error ? e.message : String(e)
    } finally {
      detecting.value = false
    }
  }

  async function save(mode: OpencodeMode, binPath: string): Promise<boolean> {
    saving.value = true
    try {
      applyStatus(await selectOpencode(mode, binPath))
      message.value = '已保存选择（config/opencode.yaml；ENV 仍可覆盖）'
      return true
    } catch (e) {
      message.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      saving.value = false
    }
  }

  async function test(binPath = ''): Promise<void> {
    testing.value = true
    try {
      lastTest.value = await testOpencode(binPath)
      message.value = lastTest.value.ok
        ? `可用：${lastTest.value.version ?? '未知版本'}（${lastTest.value.latency_ms}ms）`
        : `不可用：${lastTest.value.error ?? '未知错误'}`
    } catch (e) {
      lastTest.value = null
      message.value = e instanceof Error ? e.message : String(e)
    } finally {
      testing.value = false
    }
  }

  return { status, loading, detecting, saving, testing, message, lastTest, load, detect, save, test }
})
