import { defineStore } from 'pinia'
import { ref } from 'vue'

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

  // 监听后端状态
  if (typeof window !== 'undefined' && window.electronAPI) {
    window.electronAPI.onBackendStatus((data) => {
      if (data.status === 'ready') {
        setBackendStatus('ready', data.port)
      } else if (data.status === 'error') {
        setBackendStatus('error', undefined, (data as any).error)
      } else if (data.status === 'restarting') {
        setBackendStatus('restarting')
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
    }).catch(() => {})
  }

  return { backendStatus, backendPort, backendError, setBackendStatus }
})
