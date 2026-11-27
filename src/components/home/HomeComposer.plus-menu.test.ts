/**
 * ＋ 菜单四项级联结构的版面守卫（?raw 静态契约，与 HomeWorkspace.layout.test.ts 同风格）
 *
 * 钉住：左栏四项且常驻（附件在技能上面 —— 用户 2026-11-10 指定；附件点击直达
 * 文件选择对话框、远端文件入口移除 —— 用户 2026-11-15 指定；目标/计划两枚
 * 姿态 chip 随 M1 结构化重构退役 —— 改由会话内结构化 goal/plan 驱动）；
 * 技能 / 专家 / 插件在右栏级联展开（带搜索 + 图标/标题列表，说明一律走 title
 * 悬浮 —— 用户 2026-11-16 指定「只显示列表，不要冗余信息和小字」）；
 * 技能不显示条数数字；一级卡片静止、动效只允许出现在二级面板；落点切换留在底栏 chip。
 */
import { describe, expect, it } from 'vitest'
import rawComposer from './HomeComposer.vue?raw'
import rawAppearance from '@/composables/useAppearance.ts?raw'
import rawHomeCommand from '@/composables/useHomeCommand.ts?raw'
import rawMessageItem from '@/components/chat/MessageItem.vue?raw'
import rawQuickSsh from '@/components/ssh/QuickSshDialog.vue?raw'

const composer = rawComposer.replace(/\r\n/g, '\n')
const quickSsh = rawQuickSsh.replace(/\r\n/g, '\n')
const homeCommand = rawHomeCommand.replace(/\r\n/g, '\n')
const messageItem = rawMessageItem.replace(/\r\n/g, '\n')

/** 整个 ＋ 浮层：从双栏容器到审批档入口（ref="permRef"）之前。 */
const menuStart = composer.indexOf('class="hcc-menu cascade"')
const plusRoot = composer.slice(menuStart, composer.indexOf('ref="permRef"', menuStart))

describe('＋ 菜单左栏四项', () => {
  it('子面板的 v-else-if 链有前导 v-if（丢了会让整个 SFC 编译失败）', () => {
    const firstIf = composer.indexOf('v-if="plusPane')
    const firstElseIf = composer.indexOf('v-else-if="plusPane')
    expect(firstIf, '缺前导 v-if').toBeGreaterThan(-1)
    expect(firstElseIf, '缺 v-else-if 分支').toBeGreaterThan(-1)
    expect(firstIf).toBeLessThan(firstElseIf)
  })

  it('左栏附件 / Spec / Plan / Goal / 技能 / 专家 / 插件按序出现，且左栏常驻不整卡替换', () => {
    // 旧「目标/计划」姿态 chip 已随 M1 退役（workflow_mode 双通路删除）；
    // 用户 2026-11-23 要求加回显式入口，但语义换成「打开能力」：Spec 走任务
    // 结构化、Plan/Goal 聚焦会话级结构化面板 —— 不再下发任何姿态参数。
    const tokens = ['>附件</span>', '>Spec<', '>Plan<', '>Goal<', '>技能<', '>专家<', '>插件</span>']
    let last = -1
    for (const token of tokens) {
      const i = plusRoot.indexOf(token)
      expect(i, `＋ 左栏缺 ${token}`).toBeGreaterThan(last)
      last = i
    }
    expect(plusRoot).toContain('class="hcc-cascade-left"')
    expect(plusRoot).toContain('class="hcc-cascade-right"')
    // 附件 = 一步直达：点击直接开文件选择对话框（用户 2026-11-15），
    // 远端文件入口已移除 —— 附件按钮不带 ›、不挂 openCascade
    const pickAt = plusRoot.indexOf('@click="pickFiles"')
    const attachBtn = plusRoot.slice(
      plusRoot.lastIndexOf('<button', pickAt), plusRoot.indexOf('</button>', pickAt))
    expect(attachBtn).toContain('@click="pickFiles"')
    expect(attachBtn).not.toContain('casc-arrow')
    expect(attachBtn).not.toContain('openCascade')
    expect(plusRoot).not.toContain('>远端文件</span>')
    // 技能 / 专家 / 插件照旧走级联
    expect(plusRoot).toContain("openCascade('skill')")
    expect(plusRoot).toContain("openCascade('expert')")
    expect(plusRoot).toContain("openCascade('plugin')")
  })

  it('Spec/Plan/Goal 是能力入口而非姿态开关：无 workflow_mode、无 posture', () => {
    // 守卫红线：三个入口不得复活旧双通路（M1 删除的那套），否则服务端推导的
    // 只读档会被前端姿态覆盖，批准门重新出现逃逸口。
    // 只查实际调用点（顶部注释会提到这些旧词说明「已退役」，不算复活）
    expect(composer).not.toContain('sendWorkflowMode')
    expect(composer).not.toContain('setPosture(')
    expect(composer).not.toContain('posture:')
    // Spec = 任务结构化；Plan/Goal = emit 给父层聚焦结构化面板
    // （2026-11-23 统一语义：三入口与意图 chips 同一状态，点 = 挂/摘 chip）
    expect(plusRoot).toContain("@click=\"toggleIntentChip('spec')\"")
    expect(plusRoot).toContain("@click=\"toggleIntentChip('plan')\"")
    expect(plusRoot).toContain("@click=\"toggleIntentChip('goal')\"")
    expect(plusRoot).toContain(':class="{ current: sshAi.intentPending.spec }"')
    // 菜单项与 chips 一致：无 title 气泡（用户 2026-11-23 指正）
    const specMenuAt = plusRoot.indexOf("@click=\"toggleIntentChip('spec')\"")
    const specMenuBtn = plusRoot.slice(
      plusRoot.lastIndexOf('<button', specMenuAt), plusRoot.indexOf('</button>', specMenuAt))
    expect(specMenuBtn).not.toContain('title=')
    expect(composer).not.toContain("'stage-focus'")
    expect(composer).not.toContain('function openSpec')
    // 三个入口一步直达：不带级联箭头
    for (const name of ['Spec', 'Plan', 'Goal']) {
      const at = plusRoot.indexOf(`>${name}<`)
      const btn = plusRoot.slice(
        plusRoot.lastIndexOf('<button', at), plusRoot.indexOf('</button>', at))
      expect(btn).not.toContain('casc-arrow')
    }
  })

  it('旧 Spec 结构化入口路径已退役（2026-11-23 统一为意图 chip）', () => {
    // openSpec 三分支 toast（运行中/已有弹窗/草稿不达标）随路径整体退役：
    // 点 ＋ 菜单 Spec = 挂 spec chip，不再走发送前结构化 —— 该旁路的唯一入口
    // 回归自动浮现卡片（草稿够长够模糊时 structCanTrigger）。
    expect(composer).not.toContain('function openSpec')
    expect(composer).not.toContain('当前任务运行中，结束后再拆解')
    expect(composer).not.toContain('已有一个拆解弹窗在进行')
    expect(composer).not.toContain('先在输入框写一段需求，再用 Spec 拆解')
    // 自动浮现卡片与恢复原文仍在：结构化能力本身没有丢，只收了一个入口
    expect(composer).toContain('structCanTrigger')
    expect(composer).toContain('aria-label="恢复原文"')
  })

  it('意图先行 chips 是意图声明不是姿态开关（2026-11-23 按用户截图重做）', () => {
    // chips 行选了才显示（用户 2026-11-23 二次指正）：未挂任何东西时整行不渲染；
    // 与文字一排（用户 2026-11-23 三次指正）：overlay 叠在输入区第一行，
    // 草稿为空时右侧跟假 placeholder 灰字，真 placeholder 同步置空避免重叠
    expect(composer).toContain('v-if="hasInputChips" class="hcc-intents"')
    const intentsAt = composer.indexOf('class="hcc-intents"')
    const textareaAt = composer.indexOf('<textarea')
    // 位置红线：chips overlay 在 textarea 之前声明（同一 inputwrap 内）
    expect(intentsAt).toBeGreaterThan(-1)
    expect(intentsAt).toBeLessThan(textareaAt)
    for (const label of ['Plan', 'Spec', 'Goal']) {
      expect(composer).toContain(`label: '${label}'`)
    }
    // 渲染出来的每枚都是激活态：v-for 走 activeIntents，无未激活分支
    expect(composer).toContain('v-for="ik in activeIntents"')
    expect(composer).toContain('const activeIntents = computed(')
    expect(composer).not.toContain(':aria-pressed')
    // 技能/插件/专家 chips 同排（2026-11-23 用户指定）：在 overlay 行内、foot 无第二份
    expect(composer).toContain('v-if="hasInputChips" class="hcc-intents"')
    expect(composer).toContain('v-for="s in pickedSkills"')
    expect(composer).toContain('v-for="p in pickedPlugins"')
    expect(composer).toContain('v-if="pickedExpert"')
    expect(composer).toContain('const hasInputChips = computed(')
    expect(composer).toContain('pickedExpert.value !== null')
    const intentsBlock = composer.slice(
      composer.indexOf('class="hcc-intents"'), composer.indexOf('@paste="onPaste"'))
    expect(intentsBlock).toContain('移除技能')
    expect(intentsBlock).toContain('移除插件')
    expect(intentsBlock).toContain('移除专家')
    const afterFoot = composer.indexOf('class="hcc-foot"')
    const restAfterFoot = composer.slice(afterFoot)
    expect(restAfterFoot).not.toContain('移除技能')
    expect(restAfterFoot).not.toContain('移除插件')
    expect(restAfterFoot).not.toContain('移除专家')
    // 真同排（用户 2026-11-23 四次指正：光标要在 chips 右侧第一行）：flex 并排，
    // chips 左、textarea 右 —— 不用 overlay + 假 placeholder 的让位 hack
    expect(composer).toContain('class="hcc-inputrow"')
    expect(composer).toContain('.hcc-inputrow { display: flex')
    expect(composer).not.toContain('hcc-intents-ph')
    expect(composer).not.toContain('padding-top: calc(')
    // hover 只换 ✕ 图标（用户 2026-11-23 指正：无 title 气泡、无旁注提示）
    expect(composer).not.toContain('class="intents-hint"')
    expect(composer).toContain('.hcc-intent:hover .ico { display: none }')
    expect(composer).toContain('.hcc-intent:hover .x { display: block }')
    expect(composer).toContain('function toggleIntentChip(')
    // 视觉规范三点（2026-11-23）：① 技能/专家/插件 chip 带与 ＋ 菜单同源的常驻
    // 图标；② ico 与 ✕ 同 13×13 同槽位切换（不晃动）；③ 不铺底、字色随设置的强调色
    // ico 静态出现 4 次：意图 chip（v-for 单模板 1 处）+ 技能/插件/专家各 1 处
    expect(intentsBlock.match(/class="ico"/g)?.length).toBe(4)
    expect(intentsBlock).not.toContain('width="12"')
    expect((intentsBlock.match(/width="13"/g) || []).length).toBeGreaterThanOrEqual(8)
    const chipCss = composer.slice(
      composer.indexOf('.hcc-intent {'), composer.indexOf('.hcc-intent .x'))
    expect(chipCss).toContain('color: var(--accent-text)')
    expect(chipCss).not.toContain('background')
    expect(chipCss).not.toContain('accent-green')
    // 粗体（2026-11-23 用户指定）：无底色设计下粗体是唯一重量来源
    expect(chipCss).toContain('font-weight: 700')
    // ＋ 浮层按鼠标光标位置放置（2026-11-23 用户指定）：卡片左下角贴光标上方，
    // 二级面板只随 bottom（left 恒贴卡右侧）；无事件（键盘开）回退默认锚点
    expect(composer).toContain('@click.stop="togglePlusMenu($event)"')
    expect(composer).toContain('function anchorFromCursor(')
    expect(composer).toContain(':style="menuCardStyle"')
    expect(composer).toContain(':style="menuPanelStyle"')
    expect(composer).toContain('menuPos.value = null')
    // placeholder 恒定（真 placeholder 落在 chips 右侧，无需动态置空）
    expect(composer).toContain('placeholder="输入你的任务。"')
    // hover 只换 ✕ 图标（用户 2026-11-23 指正：无 title 气泡、无旁注提示）
    expect(composer).not.toContain('class="intents-hint"')
    expect(composer).toContain('.hcc-intent:hover .ico { display: none }')
    expect(composer).toContain('.hcc-intent:hover .x { display: block }')
    // 点卡片选项后关闭 ＋ 菜单（用户 2026-11-23）：意图/技能/插件三个挂载路径一致，
    // overlay 行上的摘除走裸 toggle*（菜单本来就关着，不受影响）
    const toggleBody = composer.slice(
      composer.indexOf('function toggleIntentChip('),
      composer.indexOf('const activeIntents'))
    expect(toggleBody).toContain('closePlusMenu()')
    expect(composer).toContain('function pickSkillFromMenu(')
    expect(composer).toContain('function pickPluginFromMenu(')
    const pickSkillBody = composer.slice(
      composer.indexOf('function pickSkillFromMenu('), composer.indexOf('function pickPluginFromMenu('))
    expect(pickSkillBody).toContain('closePlusMenu()')
    const pickPluginBody = composer.slice(
      composer.indexOf('function pickPluginFromMenu('), composer.indexOf('function pluginSummary('))
    expect(pickPluginBody).toContain('closePlusMenu()')
    expect(composer).toContain('@click="pickSkillFromMenu(s.id)"')
    expect(composer).toContain('@click="pickPluginFromMenu(p.id)"')
    // 键盘摘除（2026-11-23 用户要求）：chip 聚焦后 Delete/Backspace 即摘，
    // focus-visible 用强调色淡 glow（不硬描边）
    expect(intentsBlock.match(/keydown\.delete\.prevent/g)?.length).toBe(4)
    // 空草稿 Backspace/Delete 摘最右一枚 chip（2026-11-23 用户指定，标签输入惯例）：
    // 有字时按键归文字，只在空草稿 + 挂了 chip 时接管；摘除序与显示序相反
    const keydownBody = composer.slice(
      composer.indexOf('function onKeydown('),
      // onMounted 在组件里有多次挂载（505 行监听器 + 784 行启动），终点锚必须
      // 从起点起搜，否则命中更早的出现、切片为空串
      composer.indexOf('onMounted(() => {', composer.indexOf('function onKeydown(')))
    expect(keydownBody).toContain("e.key === 'Backspace' || e.key === 'Delete'")
    expect(keydownBody).toContain('!draft.value && hasInputChips.value')
    expect(keydownBody).toContain('removeLastInputChip()')
    const removeLastBody = composer.slice(
      composer.indexOf('function removeLastInputChip('), composer.indexOf('function onKeydown('))
    expect(removeLastBody).toContain('setSelectedExpert')
    expect(removeLastBody).toContain("'goal', 'spec', 'plan'")
    expect(composer).toContain('.hcc-intent:focus-visible')
    expect(composer.indexOf('.hcc-intent:focus-visible')).toBeLessThan(
      composer.indexOf('.hcc-intent:disabled'))
    expect(composer.indexOf('.hcc-intent:focus-visible')).toBeGreaterThan(
      composer.indexOf('.hcc-intent:hover .x'))
    // 专家同规则（2026-11-23 用户补点）：chooseExpert 本就关闭菜单，守卫防回退
    const chooseExpertBody = composer.slice(
      composer.indexOf('function chooseExpert('), composer.indexOf('function togglePlugin('))
    expect(chooseExpertBody).toContain('closePlusMenu()')
    // 红线：不下发执行参数 —— 组件里不得出现模型侧工具名（服务端推导口径不动）
    expect(composer).not.toContain('goal_propose')
    expect(composer).not.toContain('plan_propose')
    // 注入走 sendMessage 唯一通路：意图句由 intent-core 唯一事实源拼接，不新增字段
    expect(homeCommand).toContain('buildIntentSuffix(activeIntents)')
    expect(homeCommand).toContain("text + buildIntentSuffix(activeIntents)")
    // 消费一次即清：清 intent 必须发生在 sendMessage 成功之后（失败时留给用户改主意）
    const sendAt = homeCommand.indexOf('await sshAi.sendMessage(text + buildIntentSuffix')
    const clearAt = homeCommand.indexOf('clearIntentPending()')
    expect(sendAt).toBeGreaterThan(-1)
    expect(clearAt).toBeGreaterThan(sendAt)
    // 消息流 badge：用户消息从文本尾部推导（同源 intent-core），assistant 不显示
    expect(messageItem).toContain('extractIntents(content.value)')
    expect(messageItem).toContain("props.message.role === 'user'")
    expect(messageItem).toContain('class="intent-badge"')
  })

  it('技能不显示条数数字（用户 2026-11-10 指定）', () => {
    expect(plusRoot).not.toContain('casc-n')
    expect(composer).not.toContain('.casc-n')
  })

  it('右栏面板共用搜索框，技能 / 专家 / 插件按名称与描述过滤', () => {
    expect(composer).toContain('v-model="cascadeQuery"')
    expect(composer).toContain('class="casc-search"')
    expect(composer).toContain('filteredSkills')
    expect(composer).toContain('filteredExperts')
    expect(composer).toContain('filteredPlugins')
    expect(composer).toContain('function cascadeMatch(')
    // 附件面已随「一步直达」移除：右栏只有技能 / 专家 / 插件三个面板
    expect(plusRoot).not.toContain("plusPane === 'attach'")
    expect(plusRoot).not.toContain('HomeRemoteFilePicker')
    expect(plusRoot).toContain('<template v-if="plusPane === \'skill\'">')
    expect(plusRoot).toContain('<template v-else-if="plusPane === \'expert\'">')
    // 用户 2026-11-16：二级列表只显示列表 —— 每项只留名字单行，描述小字移除，
    // 辨识信息走 :title 悬浮
    expect(plusRoot).toContain('class="ci-name"')
    expect(plusRoot).not.toContain('class="ci-desc"')
  })

  it('每项只留一行主标题，说明小字改成 title（hover 才提示）', () => {
    expect(plusRoot).not.toContain('class="od"')
    // 姿态 chip 的两行 title 随 chip 一并退役，不得残留
    expect(plusRoot).not.toContain('本次要达成什么、怎么算成功')
    expect(plusRoot).not.toContain('只读拆解步骤，写操作被直接拒绝')
    expect(plusRoot).toContain(':title="pickedExpert ? `当前：${pickedExpert.name}` : \'运行策略配置包\'"')
    expect(composer).not.toContain('.hcc-opt .od {')
  })

  it('一级卡片静止不带动效，动画只允许出现在二级面板', () => {
    expect(composer).not.toContain('hcc-menu-in')
    expect(composer).toContain('casc-in')
    expect(composer).toContain(':key="plusPane"')
  })

  it('卡片尺寸固定：二级面板是卡片外的独立浮层，不撑宽也不撑高卡片', () => {
    const cardCss = composer.slice(
      composer.indexOf('.hcc-menu.cascade {'),
      composer.indexOf('.hcc-cascade-left {'),
    )
    // 卡片只有左栏这一栏，宽度写死（196 左栏 + 8 上下左右 padding）
    expect(cardCss).toContain('width: 204px')
    expect(cardCss).not.toContain('max-width: min(520px')
    // 卡片开合只看 showPlusMenu，不再被二级面板的显隐牵动
    expect(composer).toContain('v-if="showPlusMenu" class="hcc-menu cascade"')
    expect(plusRoot).toContain('v-if="showPlusMenu && plusPane !== \'root\'" class="hcc-cascade-right"')
    // 二级面板自成一张浮卡：绝对定位贴在卡片右侧同一底边
    const subCss = composer.slice(
      composer.indexOf('.hcc-cascade-right {'),
      composer.indexOf('@keyframes casc-in'),
    )
    expect(subCss).toContain('position: absolute')
    expect(subCss).toContain('left: calc(204px + 6px)')
    expect(subCss).toContain('bottom: calc(100% + 6px)')
    // 不再靠左边线与卡片分栏
    expect(subCss).not.toContain('border-left')
  })

  it('技能层在左栏常驻，右栏无数据时给去设置启用的出口', () => {
    expect(plusRoot).toContain('v-if="!skillOptions.length"')
    expect(plusRoot).toContain('openSkillSettings')
    expect(composer).toContain("openSettings('skills')")
  })

  it('技能列表在后端就绪后补拉，打开菜单时空表还能自愈重试', () => {
    expect(composer).toContain("app.readyPromise.then(() => skillsStore.loadRemoteSkills())")
    expect(composer).toContain('if (!skillsStore.enabledSkills.length) void skillsStore.loadRemoteSkills()')
  })

  it('插件面板是逐 run 勾选清单，授权仍在同意流', () => {
    // 用户 2026-11-16：插件从只读清单改为真勾选（默认全选、可逐个取消）
    expect(composer).toContain('function isPluginOn(')
    expect(composer).toContain('function togglePlugin(')
    expect(composer).toContain('sshAi.setSelectedPluginIds(current)')
    // 旧「只读行」样式与类名整体退役
    expect(composer).not.toContain('class="casc-item ro"')
    expect(composer).not.toContain('casc-item.ro')
    // 授权入口说明仍只走 title
    expect(composer).toContain('title="安装与授权在「设置 → 插件」"')
  })

  it('＋ 里不再有 SSH / 连接分组，落点全部迁到底栏 chip', () => {
    expect(plusRoot).not.toContain('connectionOpen')
    expect(plusRoot).not.toContain('<span class="ol">SSH</span>')
    expect(composer).not.toContain("plusPane === 'target'")
    expect(composer).toContain('class="hcc-landing"')
    expect(composer).toContain("landingIcon === 'local'")
    expect(composer).toContain("landingIcon === 'ssh'")
  })

  it('本地态两枚 chip：位置 + 当前文件夹；第三枚分支只在工作树出现', () => {
    // 第三枚由 showBranch 门控（store 侧只有工作树为 true，可行为测试）
    expect(composer).toContain('v-if="landing.showBranch"')
    // 本地没有分支，但有文件夹：「本地」单独一枚读不出“在哪个目录”
    expect(composer).toContain('{{ dirLabel }}')
    expect(composer).toContain('landing.activeDirLabel')
    // shell 读数彻底离开 chip：本地就是「就在这个文件夹」，PowerShell 属实现细节
    expect(composer).not.toContain('activeShellHint')
    expect(composer).not.toContain('shell-name')
    // caret 是 svg chevron（渲染稳定），不再用文本字符 ▴▾
    expect(composer).toContain('class="caret"')
    expect(composer).toContain('.caret.up { transform: rotate(180deg) }')
    expect(composer).not.toContain('▾')
  })
})

describe('附件一步直达（用户 2026-11-15 指定，取代旧「本地/远端」二级面板）', () => {
  it('附件一步直达：右栏无附件面、远端文件选择器与其回调已整体移除', () => {
    expect(plusRoot).toContain('@click="pickFiles"')
    // 远端文件入口按用户要求移除（2026-11-15）：选择器与回调都不再存在
    expect(composer).not.toContain('HomeRemoteFilePicker')
    expect(composer).not.toContain('onRemotePicked')
    // 旧二级面板的中间态已移除：attachPane / 本地文件按钮不再存在
    expect(composer).not.toContain('attachPane')
    expect(plusRoot).not.toContain("ci-name\">本地文件")
    expect(plusRoot).not.toContain('btn-primary')
  })

  it('底栏不再有独立的附件按钮与第二份浮层', () => {
    expect(composer).not.toContain('ref="attachRef"')
    expect(composer).not.toContain('showAttachMenu')
    expect(composer).not.toContain('toggleAttachMenu')
    // 点附件即开文件选择并收起 ＋ 菜单
    expect(composer).toContain('fileInputRef.value?.click()\n  closePlusMenu()')
  })

  it('＋ 菜单与落点菜单互斥，点外面各自收起', () => {
    expect(composer).toContain('if (showPlusMenu.value) {')
    expect(composer).toContain("landingMenu.value = ''")
    expect(composer).toContain('if (plusRef.value && !plusRef.value.contains(t)) showPlusMenu.value = false')
    expect(composer).toContain("if (landingRef.value && !landingRef.value.contains(t)) landingMenu.value = ''")
  })
})

describe('落点：一枚位置 + 一枚目录，工作树多一枚分支，共用一个浮层', () => {
  it('kind 面是一个 select：三行落点 + 分隔线后的两条连接入口，行首带图标', () => {
    expect(composer).toContain("landingMenu === 'kind'")
    expect(composer).toContain('<span class="ol">本地</span>')
    expect(composer).toContain('<span class="ol">云端</span>')
    expect(composer).toContain('<span class="ol">工作树</span>')
    expect(composer).toContain('<span class="ol">连接 SSH…</span>')
    expect(composer).toContain('<span class="ol">连接 WSL…</span>')
    expect(composer).toContain('class="hcc-rule"')
    expect(composer).toContain('class="tick"')
    // 云端还没有执行环境：是占位不是按钮，点了要给实话
    expect(composer).toContain('onCloudClick')
    expect(composer).toContain('云执行环境还没接入')
  })

  it('kind 面是 select：行首图标 + 右侧勾选，不写分组小标题', () => {
    // 只看 kind 面：从浮层容器切到 dir 面为止 —— chip 上的图标不在浮层里
    const kindPane = composer.slice(
      composer.indexOf('hcc-menu wide landing-menu'),
      composer.indexOf("landingMenu === 'dir' && landing.kind !== 'ssh'"),
    )
    // 行首图标与 chip 同源（本地=显示器 / 工作树=分支 / SSH=终端），2026-11-23 补齐
    expect(kindPane).toContain('<svg')
    // 组标题（已开的本地会话 / 换个目录 / 本机仓库 / git worktree 分支）全部退场
    expect(composer).not.toContain('>已开的本地会话<')
    expect(composer).not.toContain('>换个目录<')
    expect(composer).not.toContain('>本机仓库<')
    expect(composer).not.toContain('>git worktree 分支<')
  })

  it('目录面是项目选择器：一行一个打开过的目录 + 底部「＋ 新建工作区」', () => {
    expect(composer).toContain('class="casc-search dir-search"')
    expect(composer).toContain('placeholder="搜索目录"')
    expect(composer).toContain('v-for="r in dirRows"')
    // 「不在项目中工作」已撤掉（2026-11-24 用户口径）：解绑动作为第二枚 chip 行首的 ×
    expect(composer).not.toContain('title="以主目录为落点，不绑定任何文件夹"')
    expect(composer).not.toContain('atHomeRoot')
    expect(composer).toContain('function chooseHomeChip')
    // 读不出目录的本地会话不进列表，也不再有「本地终端」这种无名行
    expect(composer).not.toContain('还没有本地会话')
    expect(composer).not.toContain('>本地终端<')
    // 打开目录面时清空上次的搜索词并接管焦点
    expect(composer).toContain('dirQuery.value = \'\'')
    expect(composer).toContain('dirSearchRef.value?.focus()')
  })

  it('第二枚 chip 默认显示文件夹图标，hover 才用 × 解绑（2026-11-24 用户口径）', () => {
    expect(composer).toContain('class="hcc-dir-chip"')
    expect(composer).toContain('v-if="dirChipClearable"')
    expect(composer).toContain('@click.stop="chooseHomeChip"')
    expect(composer).toContain("const dirChipClearable = computed(() => landing.kind === 'local' && !!landing.activeBoundDir)")
    // 图标常驻（不再按绑定态切换），× 只在绑定后存在、且默认透明
    expect(composer).toContain('class="dir-ic"')
    expect(composer).not.toContain('v-if="!dirChipClearable"')
    const chipCss = composer.slice(
      composer.indexOf('.hcc-dir-chip {'),
      composer.indexOf('.hcc-opt.landing-opt'),
    )
    // × 叠在图标槽位上（绝对定位），露出靠 hover / focus-within，且不占布局、不藏键盘可达性
    expect(chipCss).toContain('position: absolute')
    expect(chipCss).toContain('opacity: 0')
    expect(chipCss).toContain('pointer-events: none')
    expect(chipCss).toContain('.hcc-dir-chip:hover .dir-chip-x')
    expect(chipCss).toContain('.hcc-dir-chip:focus-within .dir-chip-x')
    expect(chipCss).toContain('opacity: 1; pointer-events: auto')
    expect(chipCss).toContain('.hcc-dir-chip:hover .dir-chip .dir-ic')
    // 旧的两枚按钮贴合成一颗 chip 的负外边距/补位 hack 已退场
    expect(chipCss).not.toContain('margin-right: calc(-1 * var(--spacing-8))')
    expect(chipCss).not.toContain('dir-chip-x + .dir-chip')
    // 落点不铺底，灰底只是 hover 态
    expect(chipCss).toContain('.hcc-dir-chip:hover .hcc-btn { background: transparent }')
    expect(chipCss).not.toContain('color-mix')
    expect(chipCss).not.toContain('var(--bg-elevated)')
  })

  it('点第二枚 chip 不晃：选中描边用 inset box-shadow，不加尺寸（2026-11-24 实测口径）', () => {
    // Electron 无头探针实测：给 auto 尺寸的 span 加 1px border，chip 外框 +2px、整排与输入框上移 2px。
    const chipCss = composer.slice(
      composer.indexOf('.hcc-dir-chip {'),
      composer.indexOf('.hcc-opt.landing-opt'),
    )
    expect(chipCss).toContain('.hcc-dir-chip.on { box-shadow: inset 0 0 0 1px rgba(var(--accent-primary-rgb), .4) }')
    // 盒子本身不带 border（带 border 就意味着某个状态会多 2px），.on 也不再写 border 简写
    expect(chipCss).not.toMatch(/\.hcc-dir-chip(?!\.on)[^{]*\{[^}]*\bborder:\s/)
    expect(chipCss).not.toMatch(/\.hcc-dir-chip\.on\s*\{[^}]*\bborder:\s/)
  })

  it('次级面各挂自己 chip 的左缘，不再从整排 chip 左缘起开（2026-11-24 用户口径）', () => {
    expect(composer).toContain('<span ref="dirChipRef" class="hcc-dir-chip"')
    expect(composer).toContain('ref="branchChipRef"')
    const script = composer.slice(
      composer.indexOf('const dirChipRef = ref'),
      composer.indexOf('function toggleLanding('),
    )
    // left 取各自 chip 的 offsetLeft：.hcc-landing 无定位，offsetParent 就是 .hcc-sel，与浮层同一个原点
    expect(script).toContain('landingMenu.value === \'branch\'')
    expect(script).toContain('branchChipRef.value?.offsetLeft')
    expect(script).toContain('landingMenu.value === dirPane.value')
    expect(script).toContain('dirChipRef.value?.offsetLeft')
    expect(script).toContain(': undefined') // kind 面没有专属 chip，仍在整排 chip 左缘
    expect(composer).toContain(':style="landingPaneStyle"')
  })

  it('分支面列表下方不再有分隔线（2026-11-24 用户口径）', () => {
    const branchAt = composer.indexOf("v-else-if=\"landingMenu === 'branch'\"")
    const branchPane = composer.slice(branchAt, composer.indexOf('</template>', branchAt))
    expect(branchPane).not.toContain('hcc-rule')
    // 「清理失效记录」仍是条件出现，只是不再带一条空线
    expect(branchPane).toContain('v-if="hasPrunable"')
  })

  it('目录面内容左对齐：搜索行与列表行共用同一条左网格（2026-11-23 用户口径）', () => {
    // 图 1 项目选择器密度：面板更宽、行高 32；.dir-pane 只在目录面挂上
    expect(composer).toContain("'dir-pane': landingMenu === 'dir'")
    const dirCss = composer.slice(
      composer.indexOf('.hcc-sel.landing-sel .hcc-menu.wide.dir-pane'),
      composer.indexOf('/* 落点 chip：'),
    )
    expect(dirCss).toContain('width: 288px')
    expect(dirCss).toContain('min-height: 32px')
    expect(dirCss).toContain('padding-left: var(--spacing-12)')
    // 搜索行 -4 出血后内容原点落在浮层边上，左内边距 16 = 列表行的 4(浮层内边距) + 12；
    // 图标后的文字间距与列表行同源（.casc-search = 8px）—— 搜索/列表各成一条竖线
    const dirSearchCss = composer.slice(
      composer.indexOf('.landing-menu .dir-search {'),
      composer.indexOf('.dir-row-wrap'),
    )
    expect(dirSearchCss).toContain('padding: 6px var(--spacing-16) 9px')
    expect(dirSearchCss).not.toContain('var(--spacing-20)')
    const cascSearchCss = composer.slice(
      composer.indexOf('.casc-search {'),
      composer.indexOf('.casc-search:focus-within'),
    )
    expect(cascSearchCss).toContain('gap: var(--spacing-8)')
  })

  it('目录面只列自己的目录记录：不预读文件夹，记录可删', () => {
    // 候选只来自 store 的目录记录，不再从会话 / 工作树 / 仓库登记里派生
    expect(composer).toContain('...landing.recentDirs')
    expect(composer).not.toContain('localTargetOptions')
    expect(composer).not.toContain('push(landing.activeWorktree?.path')
    // 没记录时连搜索框都不出现，只剩底部两行固定入口
    expect(composer).toContain('<div v-if="landing.recentDirs.length" class="casc-search dir-search">')
    expect(composer).not.toContain('还没有可选的目录')
    // 每条记录右侧一枚 ×（独立按钮，不嵌套在行按钮里），点了只摘记录
    expect(composer).toContain('class="dir-del"')
    expect(composer).toContain('@click.stop="removeDirRecord(r.path)"')
    expect(composer).toContain('landing.removeRecentDir(path)')
  })

  it('当前目录永远在列表里并打勾：记录可能没有它（终端页开的 / 热更前就有的会话）', () => {
    // 先当前、后记录；按 dirKey（分隔符/大小写归一）去重，同一目录只有一行
    expect(composer).toContain('for (const p of [active, ...landing.recentDirs])')
    expect(composer).toContain('const k = dirKey(p)')
    expect(composer).toContain('active: k === activeKey, removable: k !== activeKey')
    // 当前项是事实不是候选：不给 ×，否则点了也不会消失
    expect(composer).toContain('v-if="r.removable"')
  })

  it('点「工作树」只换落点，不钻二级菜单：分支由底栏那枚 chip 再挑', () => {
    expect(composer).toContain('@click="pickWorktreeKind"')
    expect(composer).toContain('landing.chooseWorktreeDefault()')
    // 分支面是分支 chip 自己的浮层（并列一级），不再由「工作树」这一行点进去
    expect(composer).toContain('@click.stop="toggleLanding(\'branch\')"')
    // 次级面既已全是一级入口，就不再挂「‹ 返回」
    expect(composer).not.toContain('backToKind')
    expect(composer).not.toContain('landing-opt back')
  })

  it('SSH 走轻量弹窗：一行地址 + 主机历史，不再铺四字段表单', () => {
    expect(composer).toContain('openConnDialog')
    expect(composer).toContain('<QuickSshDialog')
    expect(composer).toContain('@close="showNewConn = false"')
    // 旧的四字段表单已退出底栏（仍在终端页保留，供完整配置用）
    expect(composer).not.toContain('<NewConnectionDialog')
    // 打开弹窗前先收浮层，两层叠在一起会互相抢焦点
    expect(composer).toContain("function openConnDialog() {\n  landingMenu.value = ''\n  showNewConn.value = true")
  })

  it('落点读数从 store 派生：位置 / 目录 / 分支各读各的', () => {
    expect(composer).toContain('class="hcc-landing"')
    expect(composer).toContain('{{ landingLabel }}')
    expect(composer).toContain('{{ dirLabel }}')
    expect(composer).toContain('{{ branchLabel }}')
    expect(composer).toContain('landing.activeRemoteLabel')
    expect(composer).toContain("landingMenu === 'repo'")
    expect(composer).toContain("landingMenu === 'branch'")
    expect(composer).toContain('chooseRepoChip(g.repo)')
    // chip 读数常驻 → 启动就得探一次 git，不能再等展开才拉
    expect(composer).toContain('void landing.loadWorktrees()')
    // 没有绑定目录时不编「主目录」：那枚 chip 写状态「不指定工作区」，动作在展开的菜单里
    expect(composer).toContain("landing.activeDirLabel || (landing.kind === 'worktree' ? '仓库' : '不指定工作区')")
  })

  it('第二枚 chip 三态各开各的面：本地/SSH 读目录，工作树读仓库', () => {
    expect(composer).toContain('toggleDirChip')
    expect(composer).toContain("landing.kind === 'worktree' ? 'repo' : 'dir'")
    expect(composer).toContain("landingMenu === 'dir' && landing.kind !== 'ssh'")
    expect(composer).toContain('chooseDirChip(r.path)')
    expect(composer).toContain('pickDirChip')
    // 「打开文件夹…」已改为「＋ 新建工作区」（用户 2026-11-15 指定，图 1 样式）
    expect(composer).not.toContain('打开文件夹…')
    expect(composer).toContain('<span class="ol">新建工作区</span>')
    const entryAt = composer.indexOf('<span class="ol">新建工作区</span>')
    const entryBtn = composer.slice(
      composer.lastIndexOf('<button', entryAt), composer.indexOf('</button>', entryAt))
    expect(entryBtn).toContain('@click="pickDirChip"')
    expect(entryBtn).toContain('M8 3v10M3 8h10')  // ＋ 图标
  })

  it('落点面连接入口唯一：SSH 主机行与「管理服务器…」退场（2026-11-23 用户口径）', () => {
    const kindPane = composer.slice(
      composer.indexOf("landingMenu === 'kind'"),
      composer.indexOf("landingMenu === 'dir' && landing.kind !== 'ssh'"),
    )
    // 连接 SSH… 是唯一连接入口（选主机进弹窗的「SSH 主机历史」）
    expect(kindPane).toContain('<span class="ol">连接 SSH…</span>')
    expect(kindPane).toContain('@click="openConnDialog"')
    // 旧的两条冲突入口退场：已存主机行（192.168.150.40 等）与「管理服务器…」
    expect(kindPane).not.toContain('sshConnectionOptions')
    expect(kindPane).not.toContain('chooseSshConn(o.id)')
    expect(kindPane).not.toContain('管理服务器…')
    // 批量下发（归第一枚 chip）没被点名删，保留
    expect(kindPane).toContain('canBulkDispatch')
    // 全链死代码清除：函数与 emit  relay 不再留在任何一层
    expect(composer).not.toContain('function chooseSshConn(')
    expect(composer).not.toContain('function openRemoteManager(')
    expect(composer).not.toContain("'remote-mgr': []")

    // 第二枚 chip（目录）的浮层里不许再出现主机清单 —— 2026-11-11 用户口径：
    // 点「目录」弹出一串主机（还带一行 user@host 勾选）不是他要的东西。
    const sshDirPane = composer.slice(
      composer.indexOf("landingMenu === 'dir' && landing.kind !== 'ssh'"),
      composer.indexOf("landingMenu === 'repo'"),
    )
    expect(sshDirPane).not.toContain('chooseSshConn')
    expect(sshDirPane).not.toContain('连接服务器…')
    expect(sshDirPane).not.toContain('管理服务器…')
  })

  it('连接 SSH 弹窗只留主机历史：手输地址框与并排连接按钮退场（2026-11-23）', () => {
    // 图三的两个冲突元素：输入框（ssh user@example.com -A）+ 右侧「连接」按钮
    expect(quickSsh).not.toContain('class="qs-addr"')
    expect(quickSsh).not.toContain('ssh user@example.com')
    expect(quickSsh).not.toContain('class="qs-go"')
    expect(quickSsh).not.toContain('function submit(')
    // 留下的是主机历史逐行连接 + 配置文件出口
    expect(quickSsh).toContain('SSH 主机历史')
    expect(quickSsh).toContain('@click="connectTo(c)"')
    expect(quickSsh).toContain('打开 SSH 配置文件')
    // 焦点管理契约不因删输入框而回退（modal-focus.guards 全仓清单仍覆盖）
    expect(quickSsh).toContain('activate()')
    expect(quickSsh).toContain('deactivate()')
  })

  it('SSH 目录面是一份真的远端文件列表：选中目录才落成 cd', () => {
    const sshDirPane = composer.slice(
      composer.indexOf("landingMenu === 'dir' && landing.kind !== 'ssh'"),
      composer.indexOf("landingMenu === 'repo'"),
    )
    // 复用通用远端选择器：列目录 + 同时列出文件（只读），用户要看到「这里有什么」
    expect(sshDirPane).toContain('<RemoteDirPicker')
    expect(sshDirPane).toContain('show-files')
    expect(sshDirPane).toContain('@update:dir="remoteDirPick = $event"')
    // 纯浏览不改落点，点「在此目录工作」才写 cd
    expect(sshDirPane).toContain('applyRemoteDir')
    expect(composer).toContain('ssh.cdRemote(id, remoteDirPick.value)')
    // 断线时先给回拨入口，不再顺手把主机列表摆出来
    expect(sshDirPane).toContain('redialActiveRemote')
  })

  it('工作树落点走 exec-target store 的 git worktree 选择', () => {
    expect(composer).toContain('useExecTargetStore')
    expect(composer).toContain('chooseWorktreeChip(w)')
    expect(composer).toContain('landing.loadWorktrees()')
  })

  it('分支面不再有「新建工作树 / 刷新工作树」入口，行内表单整套退场（2026-11-24 用户口径）', () => {
    expect(composer).not.toContain('新建工作树…')
    expect(composer).not.toContain('刷新工作树')
    expect(composer).not.toContain('wtFormOpen')
    expect(composer).not.toContain('openWtForm')
    expect(composer).not.toContain('wt-form')
  })

  it('分支面顶部不再有换仓库那一行：只剩工作树列表（2026-11-24 用户口径）', () => {
    // 从模板面的 v-else-if 起切（chip 的 :aria-expanded 也含同一串，会切错起点）
    const branchAt = composer.indexOf("v-else-if=\"landingMenu === 'branch'\"")
    const branchPane = composer.slice(
      branchAt,
      composer.indexOf('</template>', branchAt),
    )
    expect(branchPane).not.toContain('toggleLanding(\'repo\')')
    expect(branchPane).not.toContain('repoLabel')
    expect(branchPane).not.toContain('class="arrow"')
    // 全文件级：repoLabel 随之删除，不留悬空引用
    expect(composer).not.toContain('repoLabel')
    // 留下的仍必须是工作树列表本体
    expect(branchPane).toContain('chooseWorktreeChip(w)')
    expect(branchPane).toContain('hasPrunable')
  })

  it('清理失效记录只在出现失效工作树时露出（git worktree prune 不碰现存目录）', () => {
    expect(composer).toContain('hasPrunable')
    expect(composer).toContain('some(w => w.prunable)')
    expect(composer).toContain('landing.pruneWorktrees(landing.activeRepoPath)')
    expect(composer).toContain('不动任何现存目录')
  })

  it('目标 chip 仍是只读标记，× 仍是断开（不与落点 chip 抢语义）', () => {
    expect(composer).toContain('class="hcc-target"')
    expect(composer).toContain('disconnectTarget')
    expect(composer).not.toContain('showTargetMenu')
  })

  it('输入时框不变色：焦点提示只给键盘，鼠标输入靠光标', () => {
    // 用户 2026-11-27 指定：输入的时候框不要变色。:focus-within 与 :focus-visible
    // 对文本输入在鼠标点击时都命中（浏览器为 IME 故意如此，见 global.css 注），
    // 无法区分输入来源。改由 useAppearance 的 data-input-mode 判定：
    // 只在最后一次输入来自键盘时给卡面描边 + glow（WCAG 2.2 AA / DESIGN.md）。
    expect(composer).not.toContain('.hcc:focus-within')
    expect(composer).toContain(':root[data-input-mode="keyboard"] .hcc:has(.hcc-input:focus)')
    // 输入来源由全局监听写入，组件不自己挂事件（避免每个 composer 一份监听）
    expect(rawAppearance).toContain("addEventListener('keydown'")
    expect(rawAppearance).toContain("addEventListener('pointerdown'")
    expect(rawAppearance).toContain("root.dataset.inputMode = 'keyboard'")
    expect(rawAppearance).toContain("root.dataset.inputMode = 'pointer'")
  })
})
