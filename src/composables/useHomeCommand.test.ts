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
const pendingAttachments = ref<{ file_id: string; filename: string; content: string; truncated: boolean }[]>([])
const uploadingAttachments = ref(false)
/** 仿真 store.uploadAttachments：上传 + 追加 + 一把锁。真实实现在 ssh-ai.test.ts 里
 *  直接测；这里只保证 composable 调的是它、不是自己再拼一遍 spread。 */
const uploadAttachments = vi.fn(async (files: File[]) => {
  if (!files.length || uploadingAttachments.value) return 0
  uploadingAttachments.value = true
  try {
    const added = files.map(f => ({
      file_id: `id-${f.name}`, filename: f.name, content: 'x', truncated: false,
    }))
    if (added.length) pendingAttachments.value = [...pendingAttachments.value, ...added]
    return added.length
  } finally {
    uploadingAttachments.value = false
  }
})
const approvalMode = ref<'normal' | 'auto'>('normal')
const posture = ref<'goal' | 'plan' | null>(null)
const selectedSkillIds = ref<string[]>([])
const setSelectedSkillIds = vi.fn((ids: string[]) => { selectedSkillIds.value = ids })
const setPendingAttachments = vi.fn((list: typeof pendingAttachments.value) => { pendingAttachments.value = list })
// 共享草稿住 store（任务台唯一一份输入态）：mock 与真实 store 同形 —— getter 解包 +
// setSharedDraft 写回，composable 的 computed 双向绑定才能走通。
const sharedDraft = ref('')
const setSharedDraft = vi.fn((text: string) => { sharedDraft.value = text })
const isRunning = ref(false)
const info = vi.fn()
const error = vi.fn()

// pinia 会把 store 里的 ref 解包成裸值，mock 必须同形：直接返回 ref 对象的话
// `!agent.isRunning` 恒为 false（对象永远真值），测出来的是 mock 的假象。
vi.mock('@/stores/ssh-ai', () => ({
  useSshAiStore: () => ({
    sendMessage,
    uploadAttachments,
    setSelectedSkillIds,
    setPendingAttachments,
    setSharedDraft,
    get sharedDraft() { return sharedDraft.value },
    get approvalMode() { return approvalMode.value },
    get posture() { return posture.value },
    get selectedSkillIds() { return selectedSkillIds.value },
    get pendingAttachments() { return pendingAttachments.value },
    get uploadingAttachments() { return uploadingAttachments.value },
  }),
}))
vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({ get isRunning() { return isRunning.value } }),
}))
vi.mock('@/stores/toast', () => ({
  useToastStore: () => ({ info, error }),
}))

const { useHomeCommand } = await import('@/composables/useHomeCommand')

describe('useHomeCommand', () => {
  beforeEach(() => {
    sendMessage.mockClear()
    uploadAttachments.mockClear()
    setSelectedSkillIds.mockClear()
    setPendingAttachments.mockClear()
    info.mockClear()
    error.mockClear()
    approvalMode.value = 'normal'
    posture.value = null
    selectedSkillIds.value = []
    pendingAttachments.value = []
    uploadingAttachments.value = false
    sharedDraft.value = ''
    setSharedDraft.mockClear()
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

  it('下达被拒时保留技能与附件，与草稿同生同死', async () => {
    sendMessage.mockResolvedValueOnce(false)
    selectedSkillIds.value = ['sk-log']
    pendingAttachments.value = [{ file_id: 'f1', filename: 'nginx.log', content: 'boom', truncated: false }]
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(false)
    expect(selectedSkillIds.value).toEqual(['sk-log'])
    expect(pendingAttachments.value).toHaveLength(1)
  })

  it('空草稿不发送', async () => {
    const c = useHomeCommand()
    c.draft.value = '   '
    expect(await c.submit()).toBe(false)
    expect(sendMessage).not.toHaveBeenCalled()
  })

  it('只挂附件不打字也发得出去（否则文件白挂）', async () => {
    pendingAttachments.value = [{ file_id: 'f1', filename: 'nginx.log', content: 'boom', truncated: false }]
    const c = useHomeCommand()
    expect(c.canSend.value).toBe(true)
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith('', {
      approval: 'normal', mode: undefined, skillIds: [],
      attachments: [{ file_id: 'f1', filename: 'nginx.log', content: 'boom', truncated: false }],
    })
  })

  it('发送走 sendMessage，并带上 store 里的审批档', async () => {
    approvalMode.value = 'auto'
    const c = useHomeCommand()
    c.draft.value = '  把 nginx worker_connections 提到 4096  '
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '把 nginx worker_connections 提到 4096',
      { approval: 'auto', mode: undefined, skillIds: [], attachments: [] },
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
      { approval: 'normal', mode: 'plan', skillIds: [], attachments: [] },
    )
  })

  it('选中的技能随同一次下达出去（后端按 id 装目录，不是前端摆设）', async () => {
    selectedSkillIds.value = ['sk-log', 'sk-disk']
    const c = useHomeCommand()
    c.draft.value = '查一下磁盘'
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '查一下磁盘',
      { approval: 'normal', mode: undefined, skillIds: ['sk-log', 'sk-disk'], attachments: [] },
    )
  })

  it('附件随同一次下达出去，发出后与技能一起清空（都是一次性附加）', async () => {
    pendingAttachments.value = [{ file_id: 'f1', filename: 'nginx.log', content: 'boom', truncated: false }]
    const c = useHomeCommand()
    c.draft.value = '这份日志说明了什么'
    expect(await c.submit()).toBe(true)
    expect(sendMessage).toHaveBeenCalledWith(
      '这份日志说明了什么',
      {
        approval: 'normal',
        mode: undefined,
        skillIds: [],
        attachments: [{ file_id: 'f1', filename: 'nginx.log', content: 'boom', truncated: false }],
      },
    )
    expect(pendingAttachments.value).toEqual([])
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

  it('选中的文件上传成功后挂进待发送附件', async () => {
    const c = useHomeCommand()
    await c.attachFiles([new File(['boom'], 'nginx.log')])
    expect(uploadAttachments).toHaveBeenCalledTimes(1)
    expect(pendingAttachments.value.map(f => f.file_id)).toEqual(['id-nginx.log'])
    expect(c.uploading.value).toBe(false)
  })

  it('一个文件都没选时不发上传请求', async () => {
    const c = useHomeCommand()
    await c.attachFiles([])
    expect(uploadAttachments).not.toHaveBeenCalled()
  })

  it('上传整体抛错时给 toast —— 静默失败比报错更难查（单文件失败由 store 自己 toast）', async () => {
    uploadAttachments.mockRejectedValueOnce(new Error('后端 500'))
    const c = useHomeCommand()
    await c.attachFiles([new File(['x'], 'a.log')])
    expect(error).toHaveBeenCalledWith('后端 500')
    expect(pendingAttachments.value).toEqual([])
    expect(c.uploading.value).toBe(false)
  })
})
