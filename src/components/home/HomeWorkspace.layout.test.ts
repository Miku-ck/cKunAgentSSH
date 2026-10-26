/**
 * 历史记录弹窗的版面守卫（M-1 修订：右侧抽屉 → 居中弹窗）
 *
 * 弹窗 Teleport 到 body + fixed 遮罩，不再参与任务台布局。抽屉时代那套
 * 「主区/输入区为右栏让出宽度」的规则留着就是一条无效 padding —— 这里
 * 同时钉住它已退场，以及弹窗仍是一层真正的模态（role=dialog / aria-modal）。
 *
 * 另守任务台的流式滚动契约：合帧 + 贴底跟随（scroll-follow 纯函数的用例也在本文件），
 * 以及消息流裁剪契约：超长会话只渲染最新窗口、流顶「显示更早的消息」的放行与锚定。
 */
import { describe, expect, it } from 'vitest'
import { STICKY_FOLLOW_GAP_PX, shouldFollowScroll } from '@/utils/scroll-follow'
import rawWorkspace from './HomeWorkspace.vue?raw'
import rawDialog from './HomeHistoryDialog.vue?raw'

const workspace = rawWorkspace.replace(/\r\n/g, '\n')
const css = workspace.slice(workspace.indexOf('<style'))
const dialog = rawDialog.replace(/\r\n/g, '\n')

describe('历史记录弹窗的版面与模态契约', () => {
  it('任务台不再为历史面板预留右栏（抽屉让位规则已退场）', () => {
    expect(workspace).not.toContain('is-history-open')
    expect(css).not.toMatch(/\.home-main\s*\{[^}]*padding-right/)
    expect(css).not.toMatch(/\.composer-wrap\s*\{[^}]*padding-right/)
  })

  it('弹窗走 Teleport + fixed 全屏遮罩，不压在某一栏里', () => {
    expect(dialog).toContain('<Teleport to="body">')
    expect(dialog).toMatch(/\.hd-overlay\s*\{[^}]*position:\s*fixed/)
    expect(dialog).toMatch(/\.hd-overlay\s*\{[^}]*inset:\s*0/)
  })

  it('弹窗是模态：role=dialog + aria-modal=true', () => {
    expect(dialog).toContain('role="dialog"')
    expect(dialog).toContain('aria-modal="true"')
  })
})

describe('历史条目删除契约', () => {
  // 点垃圾桶就是要删，再弹一层确认等于把用户刚做的决定重问一次。
  it('删除入口直接执行，不再二次确认', () => {
    const start = workspace.indexOf('function deleteConversation')
    expect(start, '没找到 deleteConversation').toBeGreaterThan(-1)
    const body = workspace.slice(start, workspace.indexOf('\n}', start))
    expect(body).not.toMatch(/\bconfirm\(/)
    expect(body).toContain('sshAi.deleteConversation(id)')
  })
})

describe('贴底跟随判定 shouldFollowScroll', () => {
  const CH = 600
  const H = 2000
  // 距底距离 = H - CH - scrollTop

  it('视口贴底时跟随（距底 0）', () => {
    expect(shouldFollowScroll(H - CH, CH, H)).toBe(true)
  })

  it('内容不足一屏时跟随：没有可滚的距离，恒算贴底', () => {
    expect(shouldFollowScroll(0, CH, CH)).toBe(true)
    expect(shouldFollowScroll(0, CH, CH - 10)).toBe(true)
  })

  it('距底在阈值内仍跟随：流式一帧的小幅增长不致脱跟', () => {
    expect(shouldFollowScroll(H - CH - 1, CH, H)).toBe(true)
  })

  it('距底恰等于阈值算贴底（边界取含）', () => {
    expect(shouldFollowScroll(H - CH - STICKY_FOLLOW_GAP_PX, CH, H)).toBe(true)
  })

  it('用户上翻超过阈值就不跟随：流式不再抢滚动条', () => {
    expect(shouldFollowScroll(0, CH, H)).toBe(false) // 距底 1400
    expect(shouldFollowScroll(H - CH - STICKY_FOLLOW_GAP_PX - 1, CH, H)).toBe(false)
  })

  it('阈值可按场景覆盖', () => {
    expect(shouldFollowScroll(0, CH, H, 1400)).toBe(true)
    expect(shouldFollowScroll(0, CH, H, 1399)).toBe(false)
  })
})

describe('任务台流式滚动契约（rAF 合帧 + 贴底跟随）', () => {
  // 流式期间 content/reasoning 每秒触发数十次：必须走贴底跟随 + 合帧，
  // 不能退回「每次触发都无条件 nextTick + 读 scrollHeight」的老路。
  it('content/reasoning 的高频触发走贴底跟随，不再无条件跟到底', () => {
    for (const key of ['agentStore.content', 'agentStore.reasoning'] as const) {
      const at = workspace.indexOf(`watch(() => ${key}`)
      expect(at, `没找到 ${key} 的 watch`).toBeGreaterThan(-1)
      const body = workspace.slice(at, workspace.indexOf('\nwatch', at))
      expect(body).toContain('requestFollow()')
      expect(body).not.toContain('scheduleScrollToBottom()')
    }
  })

  it('一次性事件（切会话）仍无条件跟到底，贴底判定挂在 scroll 被动监听上', () => {
    const at = workspace.indexOf("watch(() => sshAi.currentConvId")
    expect(at, '没找到切会话的 watch').toBeGreaterThan(-1)
    expect(workspace.slice(at, workspace.indexOf('\nwatch', at))).toContain('scheduleScrollToBottom()')
    expect(workspace).toContain("addEventListener('scroll', onScroll, { passive: true })")
    expect(workspace).toContain('shouldFollowScroll(')
  })
})

describe('任务台消息流裁剪契约（超长会话只渲染最新窗口）', () => {
  const script = workspace.slice(workspace.indexOf('<script setup'))

  it('初始只渲染最新 80 条，窗口端锚在最新消息上', () => {
    expect(script).toContain('const INITIAL_VISIBLE_MESSAGES = 80')
    expect(script).toContain('const visibleCount = ref(INITIAL_VISIBLE_MESSAGES)')
    // 窗口必须从尾部取（slice(total - visibleCount)）：live 合并出的那条流式消息
    // 永远在窗内 —— 从头截 80 条会把正在输出的那一轮裁掉
    expect(script).toMatch(/slice\(\s*total - visibleCount\.value\s*\)/)
    expect(workspace).toContain('v-for="(msg, idx) in windowedMessages"')
  })

  it('流顶按钮文案带折叠计数，全量后随计数归零消失', () => {
    expect(workspace).toContain('显示更早的消息（还有 {{ hiddenMessageCount }} 条）')
    const at = workspace.indexOf('older-messages-btn')
    const tag = workspace.slice(
      workspace.lastIndexOf('<button', at), workspace.indexOf('</button>', at))
    expect(tag).toContain('v-if="hiddenMessageCount > 0"')
    // 计数钳在 0：放行数超过存量后不出现负数，按钮的消失条件才可靠
    expect(script).toMatch(/Math\.max\(0,\s*displayMessages\.value\.length - visibleCount\.value\)/)
  })

  it('点击每次放行 200 条，并把视口锚回新旧衔接处而不是跳底', () => {
    expect(script).toContain('const OLDER_MESSAGES_STEP = 200')
    const start = workspace.indexOf('function showOlderMessages')
    expect(start, '没找到 showOlderMessages').toBeGreaterThan(-1)
    const body = workspace.slice(start, workspace.indexOf('\n}', start))
    expect(body).toContain('visibleCount.value += OLDER_MESSAGES_STEP')
    // 展开前记内容高度、nextTick 后按高度差补 scrollTop：新增内容全部在上方，
    // 高度差正是旧内容的整体位移，补回去视口就停在衔接处
    expect(body).toContain('heightBefore')
    expect(body).toContain('el.scrollTop += el.scrollHeight - heightBefore')
    // 展开旧消息不等于来了新消息：顺手跟到底就是把用户甩到最底下
    expect(body).not.toContain('scheduleScrollToBottom')
    // Chromium 原生滚动锚定关闭：补滚只由这一套逻辑负责，两套叠加会推过头
    expect(css).toMatch(/\.home-scroll\s*\{[^}]*overflow-anchor:\s*none/)
  })

  it('换会话时裁剪窗口回到初始 80 条', () => {
    const at = workspace.indexOf('watch(() => sshAi.currentConvId')
    expect(at, '没找到切会话的 watch').toBeGreaterThan(-1)
    const body = workspace.slice(at, workspace.indexOf('\nwatch', at))
    expect(body).toContain('visibleCount.value = INITIAL_VISIBLE_MESSAGES')
  })

  it('折叠边界不拆散执行过程：窗口按整条消息切，is-live/is-last 用全量下标判定', () => {
    // 执行过程（reasoning/agentTrace）长在助手消息对象上：窗口必须是整条 slice，
    // 任何按字段挑选/重组消息的实现都会把它与所属回答拆开
    const start = workspace.indexOf('const windowedMessages')
    expect(start, '没找到 windowedMessages').toBeGreaterThan(-1)
    const body = workspace.slice(start, workspace.indexOf('\n})', start))
    expect(body).toContain('.slice(')
    expect(body).not.toMatch(/\.\.\.|pick\(|omit\(|filter\(/)
    // idx 必须换算回全量下标（hiddenMessageCount + idx）：正在输出/最后一条的判定
    // 要对着完整会话，而不是窗口内的相对位置
    expect(workspace).toContain('isLiveAt(hiddenMessageCount + idx)')
    expect(workspace).toContain('hiddenMessageCount + idx === displayMessages.length - 1')
  })
})
