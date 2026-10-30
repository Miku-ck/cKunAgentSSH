/**
 * 诊断包导出的渲染侧状态（运维页）。
 *
 * 这些动作控的是主进程行为（读 userData 下的日志、tar 打包），浏览器模式下没有对端 ——
 * 此时 `supported` 为 false，面板置灰而不是点了没反应。
 * 导出是同步重活（读日志 + 拉起 tar），所以用 `exporting` 挡重复点击：连点两次
 * 只会得到两个时间戳接近的包，用户拿哪一份都说不清。
 */
import { computed, ref } from 'vue'
import { useToastStore } from '@/stores/toast'
import type { DiagnosticsResult } from '../../shared/diagnostics-core'

function api() {
  return typeof window !== 'undefined' ? window.electronAPI : undefined
}

export function useDiagnosticsBundle() {
  const toast = useToastStore()

  const supported = computed(() => !!api()?.diagExport)
  const exporting = ref(false)
  /** 最近一次导出的结果（失败也保留：错误里的临时目录路径要能显示给用户） */
  const result = ref<DiagnosticsResult | null>(null)
  const error = ref('')

  async function exportBundle(): Promise<void> {
    const a = api()
    if (!a?.diagExport) {
      toast.warning('诊断包仅在桌面应用内可导出', 4000)
      return
    }
    if (exporting.value) return
    exporting.value = true
    error.value = ''
    try {
      const r = await a.diagExport()
      result.value = r
      if (!r.ok) {
        error.value = r.error ?? '导出失败'
        toast.error(`诊断包导出失败：${error.value}`, 6000)
      } else {
        toast.success('诊断包已导出', 3000)
      }
    } catch (e) {
      // 主进程 IPC 抛错（归一化后只剩 message）：与 result.error 同一条展示位
      const msg = e instanceof Error ? e.message : String(e)
      error.value = msg
      toast.error(`诊断包导出失败：${msg}`, 6000)
    } finally {
      exporting.value = false
    }
  }

  /** 在系统文件管理器里定位产物；包已被删/移时主进程退回产物目录 */
  async function reveal(): Promise<void> {
    const a = api()
    if (!a?.diagReveal) return
    try {
      await a.diagReveal()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e), 5000)
    }
  }

  return { supported, exporting, result, error, exportBundle, reveal }
}
