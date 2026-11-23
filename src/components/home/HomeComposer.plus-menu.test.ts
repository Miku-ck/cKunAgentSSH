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

const composer = rawComposer.replace(/\r\n/g, '\n')

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
    expect(plusRoot).toContain('@click="openSpec"')
    expect(plusRoot).toContain("@click=\"focusStage('plan')\"")
    expect(plusRoot).toContain("@click=\"focusStage('goal')\"")
    expect(composer).toContain("'stage-focus': [stage: 'plan' | 'goal']")
    // 三个入口一步直达：不带级联箭头
    for (const name of ['Spec', 'Plan', 'Goal']) {
      const at = plusRoot.indexOf(`>${name}<`)
      const btn = plusRoot.slice(
        plusRoot.lastIndexOf('<button', at), plusRoot.indexOf('</button>', at))
      expect(btn).not.toContain('casc-arrow')
    }
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
  it('kind 面是一个 select：三行落点 + 分隔线后的两条连接入口，都是纯文字行', () => {
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

  it('kind 面是 select：不带图标列，也不写分组小标题', () => {
    // 只看 kind 面：从浮层容器切到 dir 面为止 —— chip 上的图标不在浮层里
    const kindPane = composer.slice(
      composer.indexOf('hcc-menu wide landing-menu'),
      composer.indexOf("landingMenu === 'dir'"),
    )
    expect(kindPane).not.toContain('<svg')
    // 组标题（已开的本地会话 / 换个目录 / 本机仓库 / git worktree 分支）全部退场
    expect(composer).not.toContain('>已开的本地会话<')
    expect(composer).not.toContain('>换个目录<')
    expect(composer).not.toContain('>本机仓库<')
    expect(composer).not.toContain('>git worktree 分支<')
  })

  it('目录面是项目选择器：一行一个打开过的目录 + 底部「不在项目中工作」', () => {
    expect(composer).toContain('class="casc-search dir-search"')
    expect(composer).toContain('placeholder="搜索目录"')
    expect(composer).toContain('v-for="r in dirRows"')
    expect(composer).toContain('不在项目中工作')
    expect(composer).toContain('function chooseHomeChip')
    // 读不出目录的本地会话不进列表，也不再有「本地终端」这种无名行
    expect(composer).not.toContain('还没有本地会话')
    expect(composer).not.toContain('>本地终端<')
    // 打开目录面时清空上次的搜索词并接管焦点
    expect(composer).toContain('dirQuery.value = \'\'')
    expect(composer).toContain('dirSearchRef.value?.focus()')
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

  it('SSH 连接配置 / 批量下发 / 管理服务器归**第一枚** chip（落点面）', () => {
    const kindPane = composer.slice(
      composer.indexOf("landingMenu === 'kind'"),
      composer.indexOf("landingMenu === 'dir' && landing.kind !== 'ssh'"),
    )
    // SSH 面列的是连接配置（不是已开的远程会话）：选中即建 AI 专属会话，与用户终端隔离
    expect(kindPane).toContain('sshConnectionOptions')
    expect(kindPane).toContain('chooseSshConn(o.id)')
    expect(kindPane).toContain('canBulkDispatch')
    expect(kindPane).toContain('openRemoteManager')

    // 第二枚 chip（目录）的浮层里不许再出现主机清单 —— 2026-11-11 用户口径：
    // 点「目录」弹出一串主机（还带一行 user@host 勾选）不是他要的东西。
    const sshDirPane = composer.slice(
      composer.indexOf("landingMenu === 'dir'"),
      composer.indexOf("landingMenu === 'repo'"),
    )
    expect(sshDirPane).not.toContain('chooseSshConn')
    expect(sshDirPane).not.toContain('连接服务器…')
    expect(sshDirPane).not.toContain('管理服务器…')
  })

  it('SSH 目录面是一份真的远端文件列表：选中目录才落成 cd', () => {
    const sshDirPane = composer.slice(
      composer.indexOf("landingMenu === 'dir'"),
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

  it('新建工作树（参考 Trae）：行内表单 + 自动建分支 + 创建即切落点', () => {
    expect(composer).toContain('新建工作树…')
    expect(composer).toContain('wtFormInputRef')
    expect(composer).toContain('openWtForm')
    expect(composer).toContain('submitWtForm')
    expect(composer).toContain('landing.createWorktree(landing.activeRepoPath, name)')
    // 创建成功才收菜单；失败原因留在 landing.error 可见
    expect(composer).toContain('if (wt) {\n    wtFormOpen.value = false\n    landingMenu.value = \'\'\n  }')
    expect(composer).toContain('wtFormOpen.value = false')
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
})
