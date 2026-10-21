# 终端体验四件套设计：状态感知标签 / ANSI 随主题 / 强调件跟 accent / 溢出与 MRU

日期：2026-10-21
状态：待评审
来源：Design QA 走查（design-review 插件）+ 用户勾选落点 A/B/C/D，顺带修走查 minor #1/#2

## 背景与目标

终端 tab 栏功能完备（拖拽/滚轮/中键/右键/ARIA），但有三处体验债：

1. **标签无状态**：断连/重连只显示在终端内部的重连条，切走标签就看不见；后台会话有输出也无任何迹象。
2. **终端与主题脱钩**：8 套 UI 主题共用一套 GitHub Dark ANSI 16 色（`useTerminalConfig.ts` 的 `TERMINAL_DARK_THEME`），米纸手记里嵌一块冷调深色终端，色温割裂。cursor/selection/搜索高亮写死 GitHub 蓝，不随用户选的强调色。
3. **多会话导航弱**：标签溢出只有隐形横滚，无溢出列表；Ctrl+Tab 是顺序循环，不是 MRU。

目标：不改布局骨架、不动「终端恒深底」契约的前提下，把这三处补齐。

## 范围

**做**：① 状态感知标签 ② 每主题 ANSI 色板 ③ cursor/selection/搜索高亮跟 accent ④ 溢出菜单 + MRU 切换器 ⑤ 走查 minor 两枚。

**不做**（明确摘出，防范围蔓延）：
- 长命令完成标记——依赖 OSC 133 shell integration，本地 PowerShell/CMD 不发该序列，远程端需注入 shell 集成脚本，单独立项。
- 终端分屏、垂直标签栏、标签分组着色（走查清单 E/F，本轮未勾选）。
- `--bg-terminal` 恒深契约不动；终端不做浅色底。

## 设计

### ① 状态感知标签

**数据流**：

- `connStatus`：重连状态现锁在 `RemoteTerminal` 组件局部，标签栏够不着。在 ssh store `init()` 增加全局 `onSshReconnect` 订阅（与 `_setupHostKeyNotifications` 同位置同形态），按 sessionId 写入 `session.connStatus: 'ok' | 'reconnecting' | 'gave-up'`。RemoteTerminal 的状态条改读 store（单一事实源），不再自持 `reconnectState`/`reconnectGaveUp`；手动重连成功后由主进程事件统一清状态。
- `hasActivity`：LocalTerminal/RemoteTerminal 现有的 `onPtyData`/`onSshData` 处理器里，数据到达且该会话非活跃时置 `session.hasActivity = true`（store 提供 `markActivity(id)`）；`activateTerminal` 时清零。重连系统行写入不算活动。

**视觉**：标签图标左侧 6px 状态点——

| 状态 | 令牌 | 形态 |
|------|------|------|
| 后台有新输出 | `--accent-primary` | 常亮圆点 |
| 重连中 | `--status-warn-text` | 脉冲（`[data-animation="false"]` 全局兜底已覆盖，无需自处理） |
| 断连已放弃 | `--accent-red` | 常亮圆点 |

活跃标签不显示活动点（人正看着它）；重连/断连点在活跃标签上仍显示（状态比位置重要）。优先级：gave-up > reconnecting > activity。

### ② ANSI 色板随主题

`TERMINAL_DARK_THEME` 扩为 `TERMINAL_THEMES: Record<Theme, ITheme>`，`resolveTerminalTheme()` 按 `settings.appearance.theme`（system 先解析成 dark/light）取板。这是「组件局部调色板」豁免形状（同 TermSearchBar 先例：一处声明、有名字、可核对），不落 CSS 令牌、不进 variables.css。

设计原则：终端底恒深（各主题的 `--bg-terminal`），只调 16 色的色相温度与明度，向主题色温靠拢；前景统一 `#e6edf3` 档（atelier 用纸墨白 `#e8e2d5`）。

| 主题 | 终端底 | 色板故事 |
|------|--------|----------|
| dark（默认） | #000000 | **现状 GitHub 板原样保留**，零回归基线 |
| light | #17181c | 中性灰界面不配色偏：同 dark 板 |
| blue-screen | #080d16 | 冷蓝夜：蓝青提亮一档防糊底 |
| forest | #0a120d | 墨绿：绿偏苔藓暖绿，红偏朱 |
| mint | #071113 | 冷薄荷：青绿清透，整体高-key |
| bee | #0f0b05 | 暖琥珀：黄/绿偏暖，红偏橘 |
| coral | #100908 | 暖珊瑚：红/品红偏粉暖 |
| atelier | #17181c | 纸墨：低饱和记号笔调，前景 #e8e2d5 |

全色板值（16 键 × 8 主题）见文末附录 A，评审逐主题过目。

**守卫**：新增 vitest——8 主题 × 16 ANSI 键齐全；前景对各自终端底对比度 ≥4.5；ANSI 色对底 ≥2.5（彩色文本的通行下限）；dark/light 两板与现 `TERMINAL_DARK_THEME` 逐键相等（防误改基线）。

### ③ 强调件跟 accent

cursor、selection、搜索高亮（TermSearchBar 写死的三枚 GitHub 蓝）改从 `--accent-primary` 派生：运行时 `getComputedStyle(document.documentElement)` 读解析值（`applyAccent()` 内联写入后立即可得），在 accent/theme 切换的既有 watch 链上重解析终端 theme（xterm 整体重赋触发重绘，与现有背景切换同机制）。

分工边界：**accent 管 cursor / selection / 搜索高亮（用户显式选择的强调色优先）；主题色板管 16 个 ANSI 色**。两者不重叠。搜索高亮三枚派生：match = accent 40% 透明、active = accent 实色、border = accent 实色。

### ④ 溢出菜单 + MRU 切换器

**溢出**：`tabs-scroll` 出现横向溢出时（`ResizeObserver` + `scrollWidth > clientWidth` 判定），标签条右侧浮出「▾ N」按钮；点开全部会话列表（带 ① 的状态点），点击跳转并清活动标记。菜单用 NewTerminalMenu 同款 Teleport 定点模式（逃出 `overflow:hidden`，滚动即收起）。不溢出时零占位。

**MRU**：现有 Ctrl+Tab 顺序循环升级为 MRU——

- store 维护 `mruIds: string[]`，`activateTerminal` 时 touch 到栈顶；关会话剔除。
- Ctrl+Tab 按下打开切换浮层（贴标签条下缘，非居中弹层——已定），按住 Ctrl 连点 Tab/Shift+Tab 在浮层内移动高亮，松开 Ctrl 落定激活；Esc 取消回到原会话。
- 浮层条目：状态点 + 图标 + 名称，当前高亮行 `--bg-hover`。
- `nextSessionId` 的纯函数测试口径从「顺序循环」改为「MRU 序」，既有测试同步改写。

### ⑤ 走查 minor 顺带修

- `SshView.vue` 右键菜单：`border-radius: 6px` → `var(--radius-md)`；`transition: background 0.1s` → `var(--transition-fast)`。
- `NewTerminalMenu.vue` ＋按钮补 `aria-label="新建终端"`（现仅 title）。

## 错误处理与边界

- 会话关闭时清理：mruIds 剔除、connStatus/hasActivity 随 session 对象一起消亡（不单独留表）。
- `onSshReconnect` 事件里的 sessionId 找不到会话（重连完成前用户已关标签）：静默丢弃。
- accent 读取失败/为空：回落各主题色板自带的 cursor/selection 值（色板自带全套，accent 只是覆盖层）。
- 系统主题（system）运行时切换 dark/light：既有 watch 链已覆盖，终端色板跟着重解析。

## 测试

- vitest：MRU 栈行为（touch/剔除/Esc 回退）、`markActivity` 置位与 activate 清零、connStatus 事件映射（含找不到会话的丢弃）、色板守卫（附录 A 全表）、accent 派生三色、dark/light 基线逐键相等。
- 既有测试：`nextSessionId` 口径改写；RemoteTerminal 状态条改读 store 后的组件测试同步。
- 全量：`npm run typecheck` + `npm test`；改了 SshView 模板，必跑 `npm run build`（vue-tsc/vitest 不查标签平衡）。
- 视觉验收：rendererOnly + electronAPI 桩流程（本次已打通）实测三状态点、溢出菜单、MRU 浮层、8 主题终端配色。

## 附录 A：ANSI 色板全表

> dark = 现状基线（逐键不动）；light 同 dark。其余主题只列与基线不同的键，未列键继承基线。

**基线（dark / light）**：
black `#111111` · red `#f85149` · green `#3fb950` · yellow `#d29922` · blue `#58a6ff` · magenta `#bc8cff` · cyan `#39d2c0` · white `#e6edf3` · brightBlack `#6e7681` · brightRed `#ff7b72` · brightGreen `#7ee787` · brightYellow `#e3b341` · brightBlue `#79c0ff` · brightMagenta `#d2a8ff` · brightCyan `#56d4dd` · brightWhite `#f0f6fc` · foreground `#e6edf3`

**blue-screen**（冷蓝夜）：blue `#6cb6ff` · brightBlue `#8ecbff` · cyan `#4fd8e0` · brightCyan `#76e4ea` · magenta `#c8a1ff` · green `#56d364`

**forest**（墨绿）：red `#ff6a55` · green `#7ed491` · brightGreen `#97e3a8` · yellow `#e0c060` · blue `#82aaff` · cyan `#4fd6be`

**mint**（冷薄荷）：red `#fb7185` · green `#6ee7b7` · brightGreen `#8ff0c8` · yellow `#f0c674` · blue `#7dd3fc` · magenta `#c4b5fd` · cyan `#5eead4` · brightCyan `#86f0e0`

**bee**（暖琥珀）：red `#ff7a66` · green `#9ece6a` · brightGreen `#b5e085` · yellow `#ffc777` · brightYellow `#ffd892` · blue `#82aaff` · cyan `#7dcfff`

**coral**（暖珊瑚）：red `#ff8a7a` · brightRed `#ffa496` · magenta `#f0a6c0` · yellow `#f0b866` · green `#a6d189` · blue `#8caaee` · cyan `#81c8be`

**atelier**（纸墨，整体低饱和）：foreground `#e8e2d5` · white `#e8e2d5` · brightWhite `#f5efe2` · red `#d67a76` · brightRed `#e3928e` · green `#9db384` · brightGreen `#b3c79b` · yellow `#d8b26a` · brightYellow `#e6c684` · blue `#7e9cc4` · brightBlue `#98b3d6` · magenta `#b594b6` · brightMagenta `#caadcc` · cyan `#7fb5ac` · brightCyan `#99c9c0` · brightBlack `#6e6a60`

每块色板的 cursor/selection 自带基线值（`#58a6ff` / `#264f78`），运行时被 accent 覆盖（③）；accent 读取失败即回落这两值。

## 参考

走查结论与证据（对比度实测、ARIA 快照、minor 清单）见本次会话 Design QA 报告；契约文件 [DESIGN.md](../../../DESIGN.md)。
