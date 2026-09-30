/**
 * 审批档归属（spec 2026-09-29-home-command-panel · 接缝 S3 前置）
 *
 * approvalMode 原先是 SideChatPanel.vue:739 的组件局部 ref：首页输入卡要放一个
 * 审批下拉就只能自己再存一份，两处各持一档 = 第二条通路（用户在首页选「自动」，
 * 侧栏仍按「手动」发送）。提到 store 后两处读同一档。
 *
 * 刻意**不持久化** —— 与提升前一致：审批档是本次会话的意图，重启回到手动。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
    uploadChatFile: (...args: unknown[]) => uploadChatFile(...(args as [string, File])),
  },
  workflowApi: { get: vi.fn(async () => null) },
  agentApi: { grantPermission: vi.fn(async () => ({})), denyCommand: vi.fn(async () => ({})) },
  streamAgentRun: vi.fn(() => () => {}),
}))

const { useSshAiStore, pickTerminalSnapshot, ATTACHMENT_CHAR_LIMIT } = await import('@/stores/ssh-ai')

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
 * SideChatPanel.vue:649 的组件私有 ref。与审批档同一条理由：两处各存一份
 * 就是第二条通路，用户在首页点「计划」、侧栏仍按未表态发送。
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
 * 技能选择（故事 5/6 第二项）。原先是 SideChatPanel.vue:976 的组件私有 ref，
 * 首页 ＋ 菜单要点它，就同审批档/姿态一样得提进 store。
 * API 只给"覆写整份"：侧栏标签的 ×、首页 chip 的 ×、发送后清空，若各配一个
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
    // 侧栏斜杠菜单点已选中的技能是"不加"而不是"取消" —— 这不是 toggle，别改
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
 * 终端现场该不该跟着这条消息走 —— 三态折叠抽成纯函数（pickTerminalSnapshot），
 * 因为它原先长在 setup 闭包变量里：首页读不到、也关不掉（故事 5/6 第三项）。
 * 这四条就是把原先只有 askAi* 入口能走到的分支逐个钉住。
 */
describe('终端现场折叠', () => {
  const snap = (who: string) => ({ sessionId: who, selection: `sel-${who}`, cwd: `/c/${who}`, recent: [who] })
  const NONE = { sessionId: null, selection: '', cwd: '', recent: [] }

  it('未表态且开关开着 → 现场采集（今天的默认行为，一字不变）', () => {
    expect(pickTerminalSnapshot(undefined, true, () => snap('live'))).toEqual(snap('live'))
  })

  it('有暂存现场 → 用暂存的，不重采（选区登记随即被清空，只有那一刻拿得到）', () => {
    expect(pickTerminalSnapshot(snap('staged'), true, () => snap('live'))).toEqual(snap('staged'))
  })

  it('纯文本入口标了不带 → 空快照，连 cwd 与最近命令也不带', () => {
    expect(pickTerminalSnapshot(null, true, () => snap('live'))).toEqual(NONE)
  })

  it('开关关掉 → 空快照，且覆盖掉已暂存的现场（用户此刻明确说了不要）', () => {
    expect(pickTerminalSnapshot(snap('staged'), false, () => snap('live'))).toEqual(NONE)
    expect(pickTerminalSnapshot(undefined, false, () => snap('live'))).toEqual(NONE)
  })

  it('默认开关是开的 —— 提升为设置项这件事本身不改变任何既有行为', () => {
    expect(useSshAiStore().attachTerminalContext).toBe(true)
  })

  it('开关不落 localStorage（与审批档/姿态同一约定）', () => {
    const store = useSshAiStore()
    store.setAttachTerminalContext(false)
    expect(store.attachTerminalContext).toBe(false)
    expect([...lsMap.keys()].filter(k => /terminal|context|snapshot/i.test(k))).toHaveLength(0)
  })
})

/**
 * 待发送的本地附件（故事 5/6 第四项）。原先是 SideChatPanel.vue:587 的组件私有 ref，
 * 首页 ＋ 菜单要选文件就得同技能一样提进 store —— 两处各存一份的话，首页挂的文件
 * 侧栏发不出去，反之亦然。上传本身不在这一片：store.uploadFiles 早就存在。
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
    // 首页与侧栏挂的是同一份 pendingAttachments。各自一把锁时，后完成的那个会拿
    // 陈旧副本 spread，把前一个刚挂上的文件抹掉 —— 用户看到的是"文件没挂上"。
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
