import { defineStore } from 'pinia'
import { ref, onScopeDispose } from 'vue'

export const useAppStore = defineStore('app', () => {
  const backendStatus = ref<'starting' | 'ready' | 'error' | 'restarting'>('starting')
  const backendPort = ref<number>(0)
  const backendError = ref<string>('')

  function setBackendStatus(status: 'starting' | 'ready' | 'error' | 'restarting', port?: number, error?: string) {
    backendStatus.value = status
    if (port) backendPort.value = port
    if (error) backendError.value = error
    else if (status === 'ready') backendError.value = ''
  }

  // B25 修复：restarting 期间 backendPort 仍指向旧端口，request 的 8s 等待会
  // 轮询旧端口（后端已换端口监听）— 收到 restarting 后主动每 500ms 查询
  // getBackendPort()，最多 15s，及时更新 backendPort
  let _portPollTimer: ReturnType<typeof setTimeout> | null = null
  let _portPollsLeft = 0
  function stopPortPolling(): void {
    _portPollsLeft = 0
    if (_portPollTimer) {
      clearTimeout(_portPollTimer)
      _portPollTimer = null
    }
  }
  function startPortPolling(): void {
    stopPortPolling()
    _portPollsLeft = 30 // 500ms × 30 = 15s
    const api = (typeof window !== 'undefined' ? window.electronAPI : undefined)
    if (!api?.getBackendPort) return
    const poll = () => {
      _portPollTimer = null
      if (_portPollsLeft-- <= 0) return
      api.getBackendPort().then((port: number) => {
        if (port && port !== backendPort.value) {

          backendPort.value = port
        }
      }).catch((err) => {
        // 后端尚未起监听时查询失败属预期，继续轮询

      }).finally(() => {
        if (_portPollsLeft > 0) {
          _portPollTimer = setTimeout(poll, 500)
        }
      })
    }
    _portPollTimer = setTimeout(poll, 500)
  }

  // 监听后端状态
  if (typeof window !== 'undefined' && window.electronAPI) {
    const off = window.electronAPI.onBackendStatus((data) => {
      if (data.status === 'ready') {
        stopPortPolling() // ready 事件已携带新端口，无需继续轮询
        setBackendStatus('ready', data.port)
      } else if (data.status === 'error') {
        stopPortPolling()
        setBackendStatus('error', undefined, data.error)
      } else if (data.status === 'restarting') {
        setBackendStatus('restarting')
        startPortPolling()
      }
    })

    // 兜底：主动查询后端端口
    window.electronAPI.getBackendPort?.().then((port: number) => {
      if (port && backendPort.value === 0) {
        backendPort.value = port
        if (backendStatus.value === 'starting') {
          setBackendStatus('ready', port)
        }
      }
    }).catch((err) => {

    })

    // Pinia scope 销毁时清理监听器（HMR 场景）
    onScopeDispose(() => {
      stopPortPolling()
      if (typeof off === 'function') off()
    })
  }

  return { backendStatus, backendPort, backendError, setBackendStatus }
})
