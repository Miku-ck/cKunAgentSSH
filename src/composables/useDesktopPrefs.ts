/**
 * G8：桌面偏好（开机自启 / 关窗最小化到托盘 / 桌面通知）的渲染侧状态。
 *
 * 这些开关控的是 Electron 主进程行为，浏览器模式下没有对应 IPC —— 此时
 * `supported` 为 false，面板把开关置灰而不是假装能改。
 * 契约（字段/默认值/合法性）来自 shared/desktop-prefs.ts，与主进程同一份。
 */
import { computed, ref } from 'vue'
import type { DesktopPrefs } from '../../shared/desktop-prefs'
import { DEFAULT_DESKTOP_PREFS, normalizeDesktopPrefs } from '../../shared/desktop-prefs'
import { useToastStore } from '@/stores/toast'

function api() {
  return typeof window !== 'undefined' ? window.electronAPI : undefined
}

type PrefKey = keyof DesktopPrefs

/** 主进程回包可能缺字段（版本不匹配），统一过一遍 normalize 再落地 */
function asPrefs(raw: unknown): DesktopPrefs {
  return normalizeDesktopPrefs(raw)
}

export function useDesktopPrefs() {
  const toast = useToastStore()

  const prefs = ref<DesktopPrefs>({ ...DEFAULT_DESKTOP_PREFS })
  const supported = computed(() => !!api()?.setDesktopPrefs)
  const loading = ref(false)
  /** 正在写入的键：只置灰当前开关，不让整组连点 */
  const pending = ref<PrefKey | null>(null)
  const error = ref('')

  async function load(): Promise<void> {
    const a = api()
    if (!a?.getDesktopPrefs) return
    loading.value = true
    try {
      prefs.value = asPrefs(await a.getDesktopPrefs())
      error.value = ''
    } catch (e) {
      error.value = toMsg(e)
    } finally {
      loading.value = false
    }
  }

  /**
   * 开关点击：先本地翻转（UI 立刻响应），IPC 回包用主进程的权威值覆盖，
   * 失败则回滚并提示。托盘图标缺失这类拒绝来自主进程，前端不重复实现判断。
   */
  async function toggle(key: PrefKey): Promise<void> {
    const a = api()
    if (!a?.setDesktopPrefs) {
      toast.warning('桌面偏好仅在桌面应用内可改', 4000)
      return
    }
    if (pending.value !== null) return

    const prev = { ...prefs.value }
    const next = { ...prev, [key]: !prev[key] }
    prefs.value = next
    pending.value = key
    try {
      prefs.value = asPrefs(await a.setDesktopPrefs({ [key]: next[key] }))
      error.value = ''
    } catch (e) {
      prefs.value = prev
      const msg = toMsg(e)
      error.value = msg
      toast.error(`桌面偏好未生效：${msg}`, 5000)
    } finally {
      pending.value = null
    }
  }

  return { prefs, supported, loading, pending, error, load, toggle }
}

function toMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
