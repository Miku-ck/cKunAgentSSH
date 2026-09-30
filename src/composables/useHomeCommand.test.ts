/**
 * 首页指令卡的下达（spec 2026-09-29-home-command-panel · 接缝 S3）
 *
 * 唯一入口是 useSshAiStore().sendMessage —— 它包着模型有效性校验、会话创建、
 * 终端上下文采集与 resolveRunModes 装配。直接调 agentStore.runAgent 会全部绕过，
 * 所以这里锁住三件事：走 sendMessage、审批档取自 store、失败不吞草稿。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

const sendMessage = vi.fn(async () => true)
const approvalMode = ref<'normal' | 'auto'>('normal')
const posture = ref<'goal' | 'plan' | null>(null)
const selectedSkillIds = ref<string[]>([])
const setSelectedSkillIds = vi.fn((ids: string[]) => { selectedSkillIds.value = ids })
const isRunning = ref(false)
const info = vi.fn()

// pinia 会把 store 里的 ref 解包成裸值，mock 必须同形：直接返回 ref 对象的话
// `!agent.isRunning` 恒为 false（对象永远真值），测出来的是 mock 的假象。
vi.mock('@/stores/ssh-ai', () => ({
  useSshAiStore: () => ({
    sendMessage,
    setSelectedSkillIds,
    get approvalMode() { return approvalMode.value },
    get posture() { return posture.value },
    get selectedSkillIds() { return selectedSkillIds.value },
  }),
}))
vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({ get isRunning() { return isRunning.value } }),
}))
vi.mock('@/stores/toast', () => ({
  useToastStore: () => ({ info }),
}))

const { useHomeCommand } = await import('@/composables/useHomeCommand')

describe('useHomeCommand', () => {
  beforeEach(() => {
    sendMessage.mockClear()
    setSelectedSkillIds.mockClear()
    info.mockClear()
    approvalMode.value = 'normal'
    posture.value = null
    selectedSkillIds.value = []
    isRunning.value = false
  })

  it('技能是随消息的一次性附加：发出去之后清空，下一条不静默复用', async () => {
    // 审批档与姿态是常驻档（发完保留），技能不是 —— 留着会让下一条指令带上
    // 没人再要过的技能目录，而那会真改变后端装配（lifecycle.py:46）。
    selectedSkillIds.value = ['sk-log']
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(true)
    expect(selectedSkillIds.value).toEqual([])
  })

  it('下达被拒时保留技能，与草稿同生同死', async () => {
    sendMessage.mockResolvedValueOnce(false)
    selectedSkillIds.value = ['sk-log']
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(false)
    expect(selectedSkillIds.value).toEqual(['sk-log'])
  })

  it('空草稿不发送', async () => {
    const c = useHomeCommand()
    c.draft.value = '   '
    expect(await c.submit()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('发送走 sendMessage，并带上 store 里的审批档', async () => {
    approvalMode.value = 'auto'
    const c = useHomeCommand()
    c.draft.value = '  把 nginx worker_connections 提到 4096  '
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '把 nginx worker_connections 提到 4096',
      { approval: 'auto', mode: undefined, skillIds: [] },
    )
  })

  it('＋ 菜单点亮的姿态随同一次下达出去', async () => {
    // 审批档先例（be13504）已经证明：状态存在 store 里但下达时不带上，
    // 首页选的东西就只是首页的装饰 —— 姿态这条轴要防的就是同一个错。
    posture.value = 'plan'
    const c = useHomeCommand()
    c.draft.value = '看看 nginx 配置为什么报错'
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '看看 nginx 配置为什么报错',
      { approval: 'normal', mode: 'plan', skillIds: [] },
    )
  })

  it('选中的技能随同一次下达出去（后端按 id 装目录，不是前端摆设）', async () => {
    selectedSkillIds.value = ['sk-log', 'sk-disk']
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '查一下磁盘',
      { approval: 'normal', mode: undefined, skillIds: ['sk-log', 'sk-disk'] },
    )
  })

  it('发送成功后清空草稿', async () => {
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    await c.submit()
    expect(c.draft.value).toBe('')
  })

  it('发送失败（无模型 / 后端未就绪等）时保留草稿，不让用户重打', async () => {
    sendMessage.mockResolvedValueOnce(false)
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(false)
    expect(c.draft.value).toBe('查一下磁盘')
  })

  it('AI 正在运行时不排队发送，并给出明确提示', async () => {
    isRunning.value = true
    const c = useHomeCommand()
    c.draft.value = '再跑一条'
    expect(c.canSend.value).toBe(false)
    expect(await c.submit()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
    expect(info).toHaveBeenCalled()
    expect(c.draft.value).toBe('再跑一条')
  })
})
