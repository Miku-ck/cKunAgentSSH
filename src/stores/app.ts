import { defineStore } from 'pinia'
import { ref, onScopeDispose } from 'vue'

export const useAppStore = defineStore('app', () => {
  const backendStatus = ref<'starting' | 'ready' | 'error' | 'restarting'>('starting')
  const backendPort = ref<number>(0)

  function setBackendStatus(status: 'starting' | 'ready' | 'error' | 'restarting', port?: number) {
    backendStatus.value = status
    if (port) backendPort.value = port
  }

  // 启动可靠性：后端就绪闸门，供初始数据加载（会话同步/模型列表/设置）gate 用 ——
  // 消除首屏抢跑（port=0 时 getBaseUrl 抛错、每刷一次打 3 条"未就绪"warn）。
  // 主进程的 whenBackendReady 走 /api/health 轮询，这里是它在渲染层的唯一收敛点。
  //
  // 语义：首次真正就绪时 resolve（IPC resolve 或任一 backend:status 'ready' 事件，
  // 含崩溃自动重启后的 ready），一次性。**故意不 reject** —— 本次启动失败由 backendStatus
  // 诚实承载（error/restarting→ready 会自愈），若这里一次性 reject，"首次 spawn 即失败→
  // 自动重启成功"的窄窗口就会永久锁死闸门、后端已恢复却仍跳过初始加载（相较改前请求层
  // waitForBackendReady 能自愈，那是回退）。启动彻底失败时闸门不 open，被 gate 的 GET
  // 自然不发（正是要的：不打无谓请求、不刷抢跑 warn）。
  // 浏览器 dev 无 electronAPI：立即 resolve(0)，那条路径直接用 FALLBACK_PORT，
  // 抢跑失败由 request 层的 waitForBackendReady 重试兜底，不能被闸门卡死首屏。
  let _resolveReady!: (port: number) => void
  const readyPromise = new Promise<number>((res) => { _resolveReady = res })
  let _readyResolved = false
  // 幂等 resolve：IPC 与 backend:status 事件双路都可能先到，只认第一个就绪信号。
  const resolveReadyOnce = (port: number): void => {
    if (_readyResolved) return
    _readyResolved = true
    _resolveReady(port)
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
        resolveReadyOnce(data.port ?? backendPort.value) // IPC resolve 或重启后的 ready 事件，谁先到算谁
      } else if (data.status === 'error') {
        stopPortPolling()
        setBackendStatus('error') // 不锁闸门：失败态只由 backendStatus 承载，等后续 ready 事件（含自动重启）再开
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
      resolveReadyOnce(port)
    }).catch(() => {
      // 主进程本次启动已确定失败（spawn 错误 / 健康检查超时），诚实落到 error。
      // 不 reject 闸门：崩溃自动重启若成功会再发 ready 事件开启它（见上语义）。
      setBackendStatus('error')
    })

    // Pinia scope 销毁时清理监听器（HMR 场景）
    onScopeDispose(() => {
      stopPortPolling()
      if (typeof off === 'function') off()
    })
  } else {
    // 浏览器 dev（无 electronAPI）：无就绪 IPC，立即放行首屏加载 ——
    // 直接用 FALLBACK_PORT，抢跑失败由 request 层 waitForBackendReady 重试兜底。
    resolveReadyOnce(0)
  }

  return { backendStatus, backendPort, readyPromise }
})
