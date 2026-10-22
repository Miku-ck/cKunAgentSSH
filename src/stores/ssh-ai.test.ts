/**
 * 审批档归属（spec 2026-09-29-home-command-panel · 接缝 S3 前置）
 *
 * approvalMode 原先是已退役的 AI 侧栏组件的局部 ref：首页输入卡要放一个
 * 审批下拉就只能自己再存一份，两处各持一档 = 第二条通路（用户在首页选「自动」，
 * 另一处仍按「手动」发送）。提到 store 后只有这一档。
 *
 * 刻意**不持久化** —— 与提升前一致：审批档是本次会话的意图，重启回到手动。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

// 测试跑在 node 环境（vitest.config.ts），补 localStorage 最小垫片
const lsMap = new Map<string, string>()
const g = globalThis as unknown as Record<string, unknown>
g.localStorage = {
  getItem: (k: string) => lsMap.get(k) ?? null,
  setItem: (k: string, v: string) => { lsMap.set(k, v) },
  removeItem: (k: string) => { lsMap.delete(k) },
}
g.window = {
  setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
  clearTimeout: (id: unknown) => clearTimeout(id as number),
}

const uploadChatFile = vi.fn(async (_convId: string, file: File) => ({
  file_id: `id-${file.name}`,
  filename: file.name,
  content: 'x',
  truncated: false,
}))

// store 工厂在 setup 内 fire-and-forget 调 syncBackendConversations，
// 不给垫片会在每次 import 时打真网络并留下未处理的 rejection。
vi.mock('@/api/client', () => ({
  api: {
    listConversations: vi.fn(async () => []),
    getDefaultModel: vi.fn(async () => undefined),
    getMessages: vi.fn(async () => []),
    uploadChatFile: (...args: unknown[]) => uploadChatFile(...(args as [string, File])),
  },
  // 契约是「无工件返回空字符串字段，不是 null / 404」（client.ts:396），桩照此写
  workflowApi: { read: vi.fn(async () => ({ phase: '', spec: '', plan: '', goal: '' })) },
  agentApi: { grantPermission: vi.fn(async () => ({})), denyCommand: vi.fn(async () => ({})) },
  streamAgentRun: vi.fn(() => () => {}),
}))

const { useSshAiStore, pickTerminalSnapshot, missingReplies, ATTACHMENT_CHAR_LIMIT } = await import('@/stores/ssh-ai')
const { useAgentStore } = await import('@/stores/agent')
const { api } = await import('@/api/client')

describe('ssh-ai 审批档', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('默认手动审批', () => {
    expect(useSshAiStore().approvalMode).toBe('normal')
  })

  it('setApprovalMode 切换档位', () => {
    const store = useSshAiStore()
    store.setApprovalMode('auto')
    expect(store.approvalMode).toBe('auto')
    store.setApprovalMode('normal')
    expect(store.approvalMode).toBe('normal')
  })

  it('切换审批档不落 localStorage', () => {
    useSshAiStore().setApprovalMode('auto')
    expect([...lsMap.keys()].filter(k => k.includes('approval'))).toHaveLength(0)
  })
})

/**
 * 对话姿态（目标/计划）—— 故事 5 的 ＋ 菜单项要落在首页，而它原先是
 * 已退役的 AI 侧栏组件的私有 ref。与审批档同一条理由：两处各存一份
 * 就是第二条通路，用户在首页点「计划」、另一处仍按未表态发送。
 */
describe('ssh-ai 姿态轴', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('默认未表态（null），不是 goal —— 缺省 goal 会让「目标」chip 关不掉', () => {
    expect(useSshAiStore().posture).toBeNull()
  })

  it('setPosture 点亮与退出', () => {
    const store = useSshAiStore()
    store.setPosture('plan')
    expect(store.posture).toBe('plan')
    store.setPosture(null)
    expect(store.posture).toBeNull()
  })

  it('点亮计划不覆写审批档：休眠只发生在发送装配时，退出计划回到用户原档', () => {
    const store = useSshAiStore()
    store.setApprovalMode('auto')
    store.setPosture('plan')
    expect(store.approvalMode).toBe('auto')
    store.setPosture(null)
    expect(store.approvalMode).toBe('auto')
  })

  it('姿态与审批档互不落 localStorage（本次会话的意图，重启回默认）', () => {
    const store = useSshAiStore()
    store.setPosture('plan')
    store.setApprovalMode('auto')
    expect([...lsMap.keys()].filter(k => /approval|posture|mode/i.test(k))).toHaveLength(0)
  })
})

/**
 * 技能选择（故事 5/6 第二项）。原先是已退役的 AI 侧栏组件的私有 ref，
 * 首页 ＋ 菜单要点它，就同审批档/姿态一样得提进 store。
 * API 只给"覆写整份"：chip 的 ×、发送后清空，若各配一个
 * add/remove 动作，去重逻辑就会被抄成三份。
 */
describe('ssh-ai 技能选择', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('默认不预设任何技能', () => {
    expect(useSshAiStore().selectedSkillIds).toEqual([])
  })

  it('setSelectedSkillIds 覆写整份', () => {
    const store = useSshAiStore()
    store.setSelectedSkillIds(['a', 'b'])
    expect(store.selectedSkillIds).toEqual(['a', 'b'])
    store.setSelectedSkillIds(store.selectedSkillIds.filter(id => id !== 'a'))
    expect(store.selectedSkillIds).toEqual(['b'])
  })

  it('技能选择不落 localStorage —— 选了什么技能是本次会话的意图', () => {
    useSshAiStore().setSelectedSkillIds(['a'])
    expect([...lsMap.keys()].filter(k => /skill/i.test(k))).toHaveLength(0)
  })

  it('addSelectedSkill 幂等、removeSelectedSkill 摘掉（去重只有这一份实现）', () => {
    const store = useSshAiStore()
    store.addSelectedSkill('a')
    // add 的语义是"加上，已有就不重复"，不是 toggle —— 摘掉走 remove
    store.addSelectedSkill('a')
    expect(store.selectedSkillIds).toEqual(['a'])
    store.addSelectedSkill('b')
    expect(store.selectedSkillIds).toEqual(['a', 'b'])
    store.removeSelectedSkill('a')
    expect(store.selectedSkillIds).toEqual(['b'])
    store.removeSelectedSkill('不存在')
    expect(store.selectedSkillIds).toEqual(['b'])
  })
})

/**
 * 终端现场该不该跟着这条消息走 —— 三态折叠抽成纯函数（pickTerminalSnapshot）。
 * 这三条把只有 askAi* 入口能走到的分支逐个钉住。
 */
describe('终端现场折叠', () => {
  const snap = (who: string) => ({ sessionId: who, selection: `sel-${who}`, cwd: `/c/${who}`, recent: [who] })
  const NONE = { sessionId: null, selection: '', cwd: '', recent: [] }

  it('未表态 → 现场采集（直接输入时的默认行为）', () => {
    expect(pickTerminalSnapshot(undefined, () => snap('live'))).toEqual(snap('live'))
  })

  it('有暂存现场 → 用暂存的，不重采（选区登记随即被清空，只有那一刻拿得到）', () => {
    expect(pickTerminalSnapshot(snap('staged'), () => snap('live'))).toEqual(snap('staged'))
  })

  it('纯文本入口标了不带 → 空快照，连 cwd 与最近命令也不带', () => {
    expect(pickTerminalSnapshot(null, () => snap('live'))).toEqual(NONE)
  })
})

/**
 * 待发送的本地附件（故事 5/6 第四项）。原先是已退役的 AI 侧栏组件的私有 ref，
 * 首页 ＋ 菜单要选文件就得同技能一样提进 store —— 两处各存一份的话，一边挂的文件
 * 另一边发不出去。上传本身不在这一片：store.uploadFiles 早就存在。
 */
describe('ssh-ai 待发送附件', () => {
  const att = (id: string) => ({ file_id: id, filename: `${id}.log`, content: 'x', truncated: false })

  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
    uploadChatFile.mockClear()
  })

  it('默认没有附件', () => {
    expect(useSshAiStore().pendingAttachments).toEqual([])
  })

  it('uploadAttachments 追加而不是覆盖，并发第二次被同一把锁挡住', async () => {
    // pendingAttachments 只有一份，但上传锁也必须只有一把：各自一把时，后完成的那个
    // 会拿陈旧副本 spread，把前一个刚挂上的文件抹掉 —— 用户看到的是"文件没挂上"。
    const store = useSshAiStore()
    store.setPendingAttachments([att('keep')])
    const [n1, n2] = await Promise.all([
      store.uploadAttachments([new File(['a'], 'a.log')]),
      store.uploadAttachments([new File(['b'], 'b.log')]),
    ])
    expect(n1).toBe(1)
    expect(n2).toBe(0)
    expect(uploadChatFile).toHaveBeenCalledTimes(1)
    expect(store.pendingAttachments.map(f => f.file_id)).toEqual(['keep', 'id-a.log'])
    expect(store.uploadingAttachments).toBe(false)
  })

  it('没选文件时不发上传请求', async () => {
    const store = useSshAiStore()
    expect(await store.uploadAttachments([])).toBe(0)
    expect(uploadChatFile).not.toHaveBeenCalled()
  })

  it('setPendingAttachments 整份覆写（与技能同一条 API 口径）', () => {
    const store = useSshAiStore()
    store.setPendingAttachments([att('a'), att('b')])
    expect(store.pendingAttachments.map(f => f.file_id)).toEqual(['a', 'b'])
    store.setPendingAttachments(store.pendingAttachments.filter(f => f.file_id !== 'a'))
    expect(store.pendingAttachments.map(f => f.file_id)).toEqual(['b'])
  })

  it('附件不落 localStorage —— 文件内容进 store 已经很贵，跨会话留着更没道理', () => {
    useSshAiStore().setPendingAttachments([att('a')])
    expect([...lsMap.keys()].filter(k => /attach|file/i.test(k))).toHaveLength(0)
  })
})

/**
 * 远端文件挂为附件（spec 故事 5 的第五项，2026-09-30 裁决"另开一片"的那片）
 *
 * 关键约束：产出必须与 /chat/upload 的返回同形。注入通路只有一条
 * （_sendAgentMessageInner 读 att.content/att.truncated 拼进 task），远端文件
 * 若自搞一套字段，就是第二条下达通路 —— 正是这轮重构一路在防的东西。
 */
describe('ssh-ai 远端文件附件', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
    uploadChatFile.mockClear()
  })

  it('挂上后是一枚标准附件：basename 作文件名、"哪台机+路径"作身份', () => {
    const store = useSshAiStore()
    const a = store.addRemoteAttachment({ path: '/etc/nginx/nginx.conf', content: 'user nginx;', source: 'root@web-1' })
    expect(store.pendingAttachments).toEqual([
      { file_id: 'remote:root@web-1:/etc/nginx/nginx.conf', filename: 'nginx.conf', content: 'user nginx;', truncated: false, size: 11 },
    ])
    expect(a.file_id).toBe('remote:root@web-1:/etc/nginx/nginx.conf')
  })

  it('不上传 —— 内容已经在手上，没必要再往服务器磁盘拷一份', () => {
    useSshAiStore().addRemoteAttachment({ path: '/etc/hosts', content: '127.0.0.1 localhost', source: 'root@web-1' })
    expect(uploadChatFile).not.toHaveBeenCalled()
  })

  it('超过注入上限就截断并标 truncated —— 不标记，模型会把节选当全文下结论', () => {
    const store = useSshAiStore()
    const a = store.addRemoteAttachment({ path: '/var/log/big.log', content: 'x'.repeat(ATTACHMENT_CHAR_LIMIT + 7), source: 'root@web-1' })
    expect(a.content).toHaveLength(ATTACHMENT_CHAR_LIMIT)
    expect(a.truncated).toBe(true)
    // 上限与后端 routes/chat.py 的 MAX_ATTACHMENT_CHARS 同值：那边改了这个数，
    // 同一份日志"下载再上传"与"直接挂远端"会截在不同长度。
    expect(ATTACHMENT_CHAR_LIMIT).toBe(50_000)
  })

  it('同一台机同一路径再挑一次是换新内容，不是挂出第二枚 chip', () => {
    const store = useSshAiStore()
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'old', source: 'root@web-1' })
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'new', source: 'root@web-1' })
    expect(store.pendingAttachments).toHaveLength(1)
    expect(store.pendingAttachments[0].content).toBe('new')
  })

  it('换了机器就必须是另一枚 —— 只按路径去重会静默顶掉上一台那份', () => {
    // 首页目标主机可以中途换：A 机挑 /etc/hosts 后切到 B 机再挑同名文件，
    // 身份里没 host 的话 chip 数量不变、内容换掉，用户完全看不出少了 A 机那份。
    const store = useSshAiStore()
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'from-a', source: 'root@web-1' })
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'from-b', source: 'root@web-2' })
    expect(store.pendingAttachments.map(f => f.content)).toEqual(['from-a', 'from-b'])
  })

  it('chip 的 × 摘得掉远端附件 —— 与本地附件同一个 removePendingAttachment', () => {
    const store = useSshAiStore()
    const a = store.addRemoteAttachment({ path: '/etc/hosts', content: 'x', source: 'root@web-1' })
    store.removePendingAttachment(a.file_id)
    expect(store.pendingAttachments).toEqual([])
  })
})

/**
 * 换会话必须带走暂存输入（技能 chip + 待发送附件）。
 *
 * 这两份是 store 级共享状态（任务台 Composer 挂的就是这一份），
 * 而 switch/create/delete 三条切会话路径原先只清 workflowSnapshot —— 同一处注释里
 * 已经写清理由（「否则界面会显示上一个会话的 Spec/Plan/Goal，而它们并没有被注入本轮
 * 上下文」），暂存的技能与附件是同一个症状的另一半：chip 还亮着，用户按发送就把上一
 * 会话选的技能与文件发进了这一会话。附件更硬一层 —— /chat/upload 是按 backendConvId
 * 归档的（uploadFiles 的注释），A 会话上传的文件挂在 B 会话头上既落不进 B 的目录，
 * 也不会随 B 删除而清理。
 */
describe('ssh-ai 换会话带走暂存输入', () => {
  const stage = (store: ReturnType<typeof useSshAiStore>) => {
    store.addSelectedSkill('s1')
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'from-a', source: 'root@web-1' })
  }

  it('switchConversation 清掉技能与附件', async () => {
    const store = useSshAiStore()
    const a = await store.createConversation('A')
    await store.createConversation('B')
    stage(store)
    expect(store.selectedSkillIds).toEqual(['s1'])
    expect(store.pendingAttachments).toHaveLength(1)

    await store.switchConversation(a.id)

    expect(store.selectedSkillIds).toEqual([])
    expect(store.pendingAttachments).toEqual([])
  })

  it('createConversation 也清 —— 新建就是换新会话，不是接着上一会话的输入', async () => {
    const store = useSshAiStore()
    await store.createConversation('A')
    stage(store)

    await store.createConversation('B')

    expect(store.selectedSkillIds).toEqual([])
    expect(store.pendingAttachments).toEqual([])
  })

  it('删掉当前会话后不留上一会话的工件与暂存输入', async () => {
    const store = useSshAiStore()
    const a = await store.createConversation('A')
    const b = await store.createConversation('B')
    stage(store)
    store.workflowSnapshot = { phase: 'plan', spec: 'S', plan: 'P', goal: 'G' }

    await store.deleteConversation(b.id)

    const ids = store.conversations.map(c => c.id)
    expect(ids).not.toContain(b.id)
    expect(ids).toContain(a.id)
    expect(store.selectedSkillIds).toEqual([])
    expect(store.pendingAttachments).toEqual([])
    expect(store.workflowSnapshot.phase).toBe('')
  })

  it('反向锁：删的不是当前会话时，暂存输入必须留着', async () => {
    const store = useSshAiStore()
    const a = await store.createConversation('A')
    await store.createConversation('B')
    stage(store)

    await store.deleteConversation(a.id)

    expect(store.selectedSkillIds).toEqual(['s1'])
    expect(store.pendingAttachments).toHaveLength(1)
  })

  it('反向锁：不动会话时暂存输入得留着 —— 别把清空写成一次性缓冲', async () => {
    const store = useSshAiStore()
    await store.createConversation('A')
    stage(store)
    // 与"换会话"无关的动作一律不该抹掉输入：改档位、再加一枚技能、再挂一个文件
    store.setApprovalMode('auto')
    store.addSelectedSkill('s2')
    store.addRemoteAttachment({ path: '/etc/passwd', content: 'p', source: 'root@web-1' })

    expect(store.selectedSkillIds).toEqual(['s1', 's2'])
    expect(store.pendingAttachments.map(f => f.filename)).toEqual(['hosts', 'passwd'])
  })
})

/**
 * 空白任务（spec 2026-10-20-home-agent-workbench 故事 8/9）：
 * 「新建会话」只挪开指针并清掉属于上一轮的输入，不建历史条目、不中止 run。
 */
describe('ssh-ai beginNewTask', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('清掉草稿/技能/附件/姿态/工作流/未读，保留审批档、模型', async () => {
    const store = useSshAiStore()
    await store.createConversation('A')
    store.setSharedDraft('打了一半的指令')
    store.addSelectedSkill('s1')
    store.addRemoteAttachment({ path: '/etc/hosts', content: 'x', source: 'root@web-1' })
    store.setPosture('plan')
    store.workflowSnapshot = { phase: 'plan', spec: 'S', plan: 'P', goal: 'G' }
    store.unreadReply = true
    store.setApprovalMode('auto')
    store.setSelectedModel('m9')

    expect(store.beginNewTask()).toBe(true)

    expect(store.currentConvId).toBeNull()
    expect(store.sharedDraft).toBe('')
    expect(store.selectedSkillIds).toEqual([])
    expect(store.pendingAttachments).toEqual([])
    expect(store.posture).toBeNull()
    expect(store.workflowSnapshot).toEqual({ phase: '', spec: '', plan: '', goal: '' })
    expect(store.unreadReply).toBe(false)
    expect(store.approvalMode).toBe('auto')
    expect(store.selectedModelId).toBe('m9')
    // 不提前造条目：会话数仍是创建的那一条
    expect(store.conversations).toHaveLength(1)
  })

  it('运行中拒绝：返回 false 且当前会话不变（UI 禁用之外的 store 兜底）', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    const conv = await store.createConversation('A')
    agent.status = 'running'

    expect(store.beginNewTask()).toBe(false)

    expect(store.currentConvId).toBe(conv.id)
    await nextTick()
  })

  it('清掉上一轮运行通知：空白任务上不再残留黄色告警', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    await store.createConversation('A')
    agent.status = 'completed'
    agent.warnings.push('[生产强化] 危险命令在 prod 环境仅接受预授权 allowlist 放行。拒绝执行: sudo command')
    agent.errorMessage = 'boom'

    expect(store.beginNewTask()).toBe(true)

    expect(agent.warnings).toEqual([])
    expect(agent.errorMessage).toBe('')
  })
})

/**
 * 运行通知归属那条发起 run 的会话：切走/删掉它时一并清掉，
 * 否则通知会“跟到”另一条会话或空白任务上。
 */
describe('ssh-ai 换会话清运行通知', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('切换会话清掉上一轮的告警与错误', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    await store.createConversation('A')
    const b = await store.createConversation('B')
    agent.status = 'completed'
    agent.warnings.push('上一轮的黄色告警')
    agent.errorMessage = 'boom'

    await store.switchConversation(b.id)

    expect(agent.warnings).toEqual([])
    expect(agent.errorMessage).toBe('')
  })

  it('删掉当前会话同样清 —— 会话不在了，它的通知也不该留着', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    const a = await store.createConversation('A')
    await store.createConversation('B')
    // createConversation 会把指针挪到新建那条，先回到 A 再删它
    await store.switchConversation(a.id)
    agent.status = 'completed'
    agent.warnings.push('上一轮的黄色告警')

    await store.deleteConversation(a.id)

    expect(agent.warnings).toEqual([])
  })
})

/**
 * 首页可见时的未读语义（spec §5.2）：用户正看着任务台，回复已经显示在眼前，
 * 再点一颗「有新回复」的未读点就是把他已经看见的东西报成没看见。
 */
describe('ssh-ai homeVisible 与未读', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
  })

  it('回到首页（true）清未读；离开首页（false）不清', () => {
    const store = useSshAiStore()
    store.unreadReply = true

    store.setHomeVisible(false)
    expect(store.unreadReply).toBe(true)

    store.setHomeVisible(true)
    expect(store.unreadReply).toBe(false)
  })

  it('固化一轮回复时，任务台不在眼前才点亮未读', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    const conv = await store.createConversation('A')
    conv.messages.push({ id: 'temp-agent-run1', role: 'assistant', content: '' } as never)
    agent.currentConversationId = conv.id
    agent.status = 'running'
    await nextTick()

    agent.status = 'completed'
    await nextTick()

    expect(store.unreadReply).toBe(true)
  })

  it('固化这轮时用户正停在首页 → 不点亮（回复已在他眼前）', async () => {
    const store = useSshAiStore()
    const agent = useAgentStore()
    const conv = await store.createConversation('A')
    conv.messages.push({ id: 'temp-agent-run2', role: 'assistant', content: '' } as never)
    agent.currentConversationId = conv.id
    store.setHomeVisible(true)
    agent.status = 'running'
    await nextTick()

    agent.status = 'completed'
    await nextTick()

    expect(store.unreadReply).toBe(false)
  })
})

/**
 * S9-18 旧账：固化不换 id 的版本把已完成的回复当成占位，重启时整段丢弃 ——
 * 本地只剩用户消息（首页连出两个用户气泡）。后端转录是完整的，切到这类
 * 会话时要按需回填。
 */
describe('回复被吞会话的回填', () => {
  const STORAGE_KEY = 'ckun-ssh-ai-conversations'

  beforeEach(() => {
    setActivePinia(createPinia())
    lsMap.clear()
    vi.mocked(api.getMessages).mockReset()
  })

  it('missingReplies 只认「有消息但没回复」，空会话不算', () => {
    expect(missingReplies([])).toBe(false)
    expect(missingReplies([{ role: 'user' }])).toBe(true)
    expect(missingReplies([{ role: 'user' }, { role: 'assistant' }])).toBe(false)
  })

  it('切到只剩用户气泡的会话：后端带回回复就回填', async () => {
    lsMap.set(STORAGE_KEY, JSON.stringify([{
      id: 'c1', title: 'T', updated_at: 1, backendConvId: 'b1',
      messages: [
        { id: 'u1', role: 'user', content: '你好' },
        { id: 'u2', role: 'user', content: '你好' },
      ],
    }]))
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      { id: 'bu1', role: 'user', content: '你好', created_at: '' },
      { id: 'ba1', role: 'assistant', content: '回复一', created_at: '', agent_trace: { status: 'completed' } },
      { id: 'bu2', role: 'user', content: '你好', created_at: '' },
      { id: 'ba2', role: 'assistant', content: '回复二', created_at: '' },
    ] as never)
    const store = useSshAiStore()

    await store.switchConversation('c1')

    const conv = store.conversations.find((c: any) => c.id === 'c1') as any
    expect(conv.messages.map((m: any) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant'])
    // 后端是 snake_case 的 agent_trace，UI 读 camelCase 的 agentTrace
    expect(conv.messages[1].agentTrace).toEqual({ status: 'completed' })
  })

  it('后端也没有回复（run 跑到一半断了）→ 不动本地那份', async () => {
    lsMap.set(STORAGE_KEY, JSON.stringify([{
      id: 'c1', title: 'T', updated_at: 1, backendConvId: 'b1',
      messages: [{ id: 'u1', role: 'user', content: '你好' }],
    }]))
    vi.mocked(api.getMessages).mockResolvedValueOnce([
      { id: 'bu1', role: 'user', content: '你好', created_at: '' },
    ] as never)
    const store = useSshAiStore()

    await store.switchConversation('c1')

    const conv = store.conversations.find((c: any) => c.id === 'c1') as any
    expect(conv.messages.map((m: any) => m.id)).toEqual(['u1'])
  })
})
