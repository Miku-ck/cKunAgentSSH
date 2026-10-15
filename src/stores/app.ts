import { defineStore } from 'pinia'
import { ref, onScopeDispose } from 'vue'

export const useAppStore = defineStore('app', () => {
  const backendStatus = ref<'starting' | 'ready' | 'error' | 'restarting'>('starting')
  const backendPort = ref<number>(0)

  function setBackendStatus(status: 'starting' | 'ready' | 'error' | 'restarting', port?: number) {
    backendStatus.value = status
    if (port) backendPort.value = port
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
      }).catch(() => {
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
        setBackendStatus('error')
      } else if (data.status === 'restarting') {
        setBackendStatus('restarting')
        startPortPolling()
      }
    })

    // 兜底：backend:status 事件可能早于渲染进程挂载就发过了，这里主动补一次。
    // ⚠️ 端口只当 URL 提示（client.ts 的 getBaseUrl 需要它），不据此判定 ready ——
    // 主进程的 this.port 在 spawn【之前】就分配好了，"查到非零端口"≠"有人在那个端口
    // 监听"。旧写法在这里直接把 starting 提成 ready，于是后端彻底起不来时每次首屏都
    // 谎报就绪（且 this.port 失败路径不归零，谎报会一直复现）。真就绪一律等
    // backend:whenReady，它内部走的是健康检查。
    window.electronAPI.getBackendPort?.().then((port: number) => {
      if (port && backendPort.value === 0) {
        backendPort.value = port
      }
    }).catch(() => { /* 端口提示拿不到不影响 ready 事件那条主路径 */ })

    window.electronAPI.whenBackendReady?.().then((port: number) => {
      setBackendStatus('ready', port)
    }).catch(() => {
      // 主进程本次启动已确定失败（spawn 错误 / 健康检查超时），诚实落到 error
      setBackendStatus('error')
    })

    // Pinia scope 销毁时清理监听器（HMR 场景）
    onScopeDispose(() => {
      stopPortPolling()
      if (typeof off === 'function') off()
    })
  }

  return { backendStatus, backendPort }
})
