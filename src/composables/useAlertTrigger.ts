/**
 * 告警触发诊断 — vue 接线（SshView setup 挂一行 `useAlertTrigger()` 即生效）
 *
 * 与 composables/useSshNavigation.ts 的监控生命周期 watch（:366-379）并行共存，
 * 双方零改动：本模块只读 useServerMonitorStore 导出的状态（current/polling/
 * cpuHistory/memHistory），不 start/stop 轮询。
 *
 * 执行链（全部既有 API，零新端点）：
 *   连续 N 次越限（utils/alert-trigger-core.ts 状态机）
 *     → api.createConversation('[告警诊断] {host} {规则id}', 'ssh')  // 专用会话，
 *       避开与用户交互会话撞后端同会话并发 409（routes/agent.py:990-997）
 *     → agentStore.runAgent({ task, conversationId, modelId, systemPrompt,
 *         sshSessionId: current.sshId, environment: 'test', permissionMode: 'normal',
 *         maxCostUsd: 设置默认 })
 *       —— 服务端 resolve_run_environment 按主机连接配置硬判定、客户端声明仅作上限
 *       （routes/agent.py:999-1005，只收紧），prod 主机自动强化；
 *       变更操作照常弹审批卡（permission_needed → ApprovalDock），无需新治理代码。
 *
 * 防风暴四层闸（设计 §1.1）：N 次计数本身（core）、冷却期 + in-flight（core）、
 * isRunning 拦截（用户 run 进行中绝不发起 —— runAgent 会 abort 旧流，agent.ts:314-318）、
 * 后端同会话 409 硬闸（专用会话规避）。冷却以触发时刻起算：run 失败（SSE 停滞看门狗 /
 * 后端重启 / 409）照常计时，否则每次失败重试都会重置风暴窗口。
 *
 * run 级 409 在 SSE 流内部由 agentStore 既有 onError 消化，本模块无从观测 ——
 * 能做的「静默放弃」即：不 toast、不重试，冷却已按触发时刻计时。
 */
import { getCurrentScope, onScopeDispose, watch } from 'vue'
import { useServerMonitorStore } from '@/stores/server-monitor'
import { useAgentStore } from '@/stores/agent'
import { useAppStore } from '@/stores/app'
import { useModelsStore } from '@/stores/models'
import { useSettingsStore } from '@/stores/settings'
import { useToastStore } from '@/stores/toast'
import { api } from '@/api/client'
import {
  ALERT_CONFIG_STORAGE_KEY,
  ALERT_SYSTEM_PROMPT,
  buildDiagnosisTaskText,
  createAlertRuntime,
  defaultAlertConfig,
  evaluateRules,
  isHostEnabled,
  loadAlertConfig,
  markTriggered,
  METRIC_LABELS,
  processSample,
  resetAlertRuntime,
  saveAlertConfig,
  setInFlight,
  type AlertRule,
  type AlertTriggerConfig,
} from '@/utils/alert-trigger-core'
import type { ServerMetrics } from '@/stores/server-monitor-core'

/**
 * 解析默认模型（先例 ssh-ai.ts:759-763 getDefaultModelId 的最小复刻）：
 * 后端默认档 → 模型列表首个；都取不到返回 undefined（调用方静默放弃本轮）。
 */
async function resolveDefaultModelId(): Promise<string | undefined> {
  const modelsStore = useModelsStore()
  if (modelsStore.models.length === 0) await modelsStore.fetchModels()
  const list = modelsStore.models
  const usable = (id?: string) => !!id && (!list.length || list.some(m => m.id === id))
  try {
    const def = await api.getDefaultModel()
    if (usable(def?.id)) return def.id
  } catch { /* 默认档取不到（后端未就绪等），回落列表首个 */ }
  return list[0]?.id
}

/**
 * 挂载告警触发。返回停止函数（组件卸载即停；测试用它清理 watcher）。
 * 配置在挂载时读取一次：v1 无规则编辑 UI，手改 localStorage 生效于下次挂载。
 */
export function useAlertTrigger(): () => void {
  const monitorStore = useServerMonitorStore()
  const agentStore = useAgentStore()
  const appStore = useAppStore()
  const toast = useToastStore()
  const settingsStore = useSettingsStore()

  const config: AlertTriggerConfig = loadAlertConfig()
  // 预置规则落盘一次（键不存在时）：v1 靠手改 localStorage 调规则，得让用户看得到模板
  try {
    if (typeof localStorage !== 'undefined' && localStorage.getItem(ALERT_CONFIG_STORAGE_KEY) === null) {
      saveAlertConfig(defaultAlertConfig(), localStorage)
    }
  } catch { /* 预置落盘失败不影响内存内判断 */ }

  const runtime = createAlertRuntime()
  /** 当前监控主机（host 变化 = 切会话 / stop，计数与冷却一并清零，见 watch） */
  let activeHost = ''

  // 跨 tick 单飞闸：trigger 的建会话/解析模型链路耗时的窗口内，下一 tick 的
  // 另一条候选也会通过 isRunning 检查 —— 后发 runAgent 会 abort 先发的诊断流
  // （agent.ts:314-318）。runAgent 调用后即解除（此后由 isRunning 闸接管）。
  let launchBusy = false

  function trigger(host: string, rule: AlertRule, metrics: ServerMetrics): void {
    // 冷却以触发时刻起算：先落冷却与在飞标记，再走异步链 —— 建会话/解析模型失败也
    // 照常计时，避免每 5s 重试形成风暴（设计 §1.2「run 失败照常计时」）
    markTriggered(runtime, host, rule.id, Date.now(), config.cooldownMinutes)
    setInFlight(runtime, host, rule.id, true)
    launchBusy = true
    // 触发时刻同步抓取历史尾部：建会话期间用户可能切会话（历史被切走/清零），
    // 异步后从 store 读会把另一台主机的序列塞进这台的诊断文本
    const cpuHistory = [...monitorStore.cpuHistory]
    const memHistory = [...monitorStore.memHistory]
    void (async () => {
      try {
        const conv = await api.createConversation(`[告警诊断] ${host} ${rule.id}`, 'ssh')
        if (!conv?.id) throw new Error('创建告警诊断会话失败：后端未返回会话 id')
        const modelId = await resolveDefaultModelId()
        if (!modelId) throw new Error('告警诊断无可用模型')
        // 用户 run 已抢先（建会话窗口内开始）：静默放弃 —— runAgent 会 abort 旧流，
        // 绝不能顶掉用户正在看的 run（agent.ts:314-318）
        if (agentStore.isRunning) return
        const agent = settingsStore.settings?.agent
        agentStore.runAgent({
          task: buildDiagnosisTaskText({ host, rule, metrics, cpuHistory, memHistory }),
          conversationId: conv.id,
          modelId,
          systemPrompt: ALERT_SYSTEM_PROMPT,
          sshSessionId: metrics.sshId,
          // 客户端声明仅作上限：服务端 resolve_run_environment 按连接配置硬判定、只收紧
          environment: 'test',
          permissionMode: 'normal',
          maxCostUsd: agent?.breakerDisabled ? 0 : (agent?.maxCostUsd || undefined),
        })
        launchBusy = false
        toast.info(`指标告警：${host} ${METRIC_LABELS[rule.metric]}连续越限，已在任务台自动发起诊断`, 6000)
      } catch {
        // 静默放弃：不 toast、不重试。冷却已按触发时刻计时，风暴窗口不会因失败重置
      } finally {
        setInFlight(runtime, host, rule.id, false)
        launchBusy = false
      }
    })()
  }

  const stopWatch = watch(() => monitorStore.current, (cur) => {
    // stop()（会话全关 → current=null）或切会话（host 变化）：计数与冷却一并清零。
    // 切回原主机也需重新累积 N 个样本 —— 拿保留快照里的陈旧计数触发等于误报。
    const host = cur?.host ?? ''
    if (host !== activeHost) {
      resetAlertRuntime(runtime)
      activeHost = host
    }
    if (!cur || !host) return
    // 采集连败转后台重试的间隙（enterRetryMode → polling=false）：样本冻结不喂计数，
    // 连续计数以最后一次成功样本为准，不因时间流逝虚增（重试成功的那一拍 polling
    // 尚未翻回 true，同样跳过，下个 5s 样本自然续上）
    if (!monitorStore.polling) return
    if (!isHostEnabled(config, host)) return

    const evals = evaluateRules(config.rules, cur)
    const candidates = processSample(runtime, host, evals, Date.now())
    // 单 tick 单发起（launched）+ 跨 tick 单飞（launchBusy）：runAgent 在 trigger
    // 的异步链里要等建会话/解析模型之后才调用，这段窗口内 isRunning 还未置位 ——
    // 不加闸第二条候选会发起并把第一条的流 abort 掉（agent.ts:314-318）。
    // 未发起的候选不落冷却（continue 在 trigger 之前），计数仍 ≥N，条件成立自然补上。
    let launched = false
    for (const rule of candidates) {
      // 用户 run 进行中 / 后端未就绪 / 已有触发在建会话窗口：静默放弃本轮，
      // 不设冷却（计数已累积，条件仍成立时后续 tick 自然再评估）
      if (launched) continue
      if (launchBusy) continue
      if (agentStore.isRunning) continue
      if (appStore.backendStatus !== 'ready') continue
      launched = true
      trigger(host, rule, cur)
    }
  })

  const stop = () => stopWatch()
  if (getCurrentScope()) onScopeDispose(stop)
  return stop
}
