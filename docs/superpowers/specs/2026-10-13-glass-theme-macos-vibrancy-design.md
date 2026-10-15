# 玻璃主题重构：升级为一对 macOS 材质主题（玻璃 / 浅墨）

日期：2026-10-13
状态：**已被 `2026-10-14-aero-glass-theme-design.md` 取代**（玻璃两档的材质已从 macOS vibrancy 换成 Windows 7 Aero）。本文件保留价值在 §实施回填：`--canvas-bg` 全透壁纸模型、body 叠层式背景的根因、唯二例外清单与浅底翻牌扩展 —— 那四条在 Aero 版里原样沿用，只有色相/圆角/阴影形状换了口径。「浅墨」这一档现称**晶白**。

## 背景与目标

用户反馈：现有 `data-theme="glass"` 根本不是想要的效果，目标是**苹果风格的玻璃材质**。
经视觉伴侣两轮对比，选定 **macOS 材质 vibrancy**（克制、中性、柔和分层高光），并要两版：

- **玻璃（`glass`）**：暗调浅玻璃 —— macOS 深色模式 vibrancy。
- **浅墨（`glass-light`）**：浅色亮玻璃 —— macOS 浅色模式 vibrancy，深墨字。

## 根因（现状为何"不像苹果玻璃"）

两处机制叠加，把玻璃压成了"发闷的深色 glassmorphism"：

1. **结构画布遮挡渐变**：`body` 画的是主题渐变（`--theme-gradient`），但
   `.titlebar / .ssh-view / .term-tabs` 这些上色的结构画布默认 `--canvas-bg = var(--bg-base)`
   是**不透明**的，把 body 渐变整片盖死。面板的 `backdrop-filter` 背后只剩一块死黑，
   只能洇出中性灰 —— 没有"背后颜色透上来"的折射，就没有苹果玻璃感。
2. **渐变开启反而更不透**：通用 `[data-gradient="on"]` 用
   `color-mix(--bg-elevated-solid … var(--gradient-alpha))` 派生面板底，`--gradient-alpha`
   最低也只到 80%（强度 100 时），即面板最多只有 20% 透明；再叠 `backdrop-filter` 也看不出折射。

**解法（本次重构的核心机制修复）**：

- 玻璃主题里把 `--canvas-bg` 改为 **`transparent`**，让 body 渐变透到结构画布之下、
  再被半透明面板的 `backdrop-filter` 折射 → 真正的彩色毛玻璃。
- 玻璃主题**自带 `[data-theme="glass(-light)"][data-gradient="on"]` 高优先级覆盖**，把
  `--bg-elevated / --bg-input` 直接重设为半透明白/半透明浅底，**绕开通用 color-mix 的
  高不透明派生**，使面板在渐变模式下真正通透。（该覆盖选择器特异度 `(0,2,0)` 高于通用
  `[data-gradient="on"]` 的 `(0,1,0)`，稳定生效。）

## 改动范围

| 文件 | 改动 |
|---|---|
| `src/utils/appearance-tokens.ts` | `Theme` 联合类型追加 `'glass-light'` |
| `src/stores/settings.ts` | `THEMES` 白名单数组追加 `'glass-light'`（`asEnum` 兜底依赖它） |
| `src/components/settings/panels/AppearancePanel.vue` | `themeLabels` 补 `'glass-light': '浅墨'`（`Record<Theme,…>` 穷举会强制）；主题行下加一条**静态内联提示**（`v-if` theme 为 `glass`/`glass-light` 且渐变关时显示「开启主题渐变色可获得玻璃折射效果」，纯文案、不改用户设置） |
| `src/monaco-setup.ts` | **补齐 `THEME_MAP`**：现状缺 `glass` 一档（穷举已隐患）；加 `glass: 'ckun-dark'`、`glass-light: 'ckun-light'` |
| `src/styles/variables.css` | 重写 `[data-theme="glass"]` 为 macOS 暗调 vibrancy；新增 `[data-theme="glass-light"]` 浅底 vibrancy；各配 `[data-gradient="on"]` 覆盖块 |
| `src/styles/global.css` | 玻璃折射层选择器扩展到两个主题，按 macOS 克制分档设 `backdrop-filter` 参数 |

明确不做（YAGNI / 约束）：

- **不新增任何 `:root` 令牌**（只覆盖既有令牌），避免 `variables.test.ts`「无消费方 = 失效设置项」红。
- 不改版面尺寸 / 布局（`--titlebar-h` / `--tabbar-h` 等归版面，主题不碰）。
- 不引入第三个背景层级（守 `variables.css` 头部「只有两级」约定）。
- 不改已 tokenized 的组件样式（终端页/设置族一律读令牌，无需主题分支）。
- 不做 macOS 平台专属的 Electron 窗口级 `vibrancy`（跨平台限制）；统一用「app 内渐变 +
  `backdrop-filter`」方案在 Windows 上成立。
- **不加运行时 toast/强改用户设置**：渐变关闭时玻璃退化为优雅的素色毛玻璃（仍可看）。
  仅在设置面板主题行下留一条 `v-if` 静态提示（见改动范围），沿用本仓「把状态说破、不隐式改设置」的约定。

## 令牌层设计（variables.css）

### 玻璃（暗调 · `data-theme="glass"`）

- 层级：`--bg-base: #14161c`（深板岩，非纯黑，给"无渐变"兜底一点层次）；`--canvas-bg: transparent`
- 面板：`--bg-elevated: rgba(255,255,255,0.08)`；`--bg-elevated-solid: #23262f`；
  `--bg-input: rgba(255,255,255,0.05)`；`--bg-input-solid: #1d2029`
- 交互：`--bg-hover: rgba(255,255,255,0.10)`；`--bg-active: rgba(255,255,255,0.16)`
- 分隔/描边：`--divider-primary: rgba(255,255,255,0.10)`；`--border-default: rgba(255,255,255,0.20)`；
  `--border-subtle: rgba(255,255,255,0.12)`；`--border-input: rgba(255,255,255,0.16)`；`--bg-slider: rgba(255,255,255,0.18)`
- 文字：`--text-primary: #f5f6fa`；`--text-secondary: rgba(245,246,250,0.74)`；`--text-tertiary: rgba(245,246,250,0.56)`
- 强调色（降一档饱和、更耐看，pastel 但不刺眼）：cyan `#7dd3fc` / purple `#c4b5fd` / orange `#fdba74` /
  green `#86efac` / red `#fca5a5` / yellow `#fde68a`；indigo `#a5b4fc(-soft #c7d2fe)`；pink `#f0abfc`
- 隧道徽标 tint：l `#7dd3fc` / r `#c4b5fd` / d `#a5b4fc`
- status-*：透明底 + 饱和字（沿用现 glass 形状，色相迁到新 pastel）
- 圆角/阴影：`--radius-sm 6 / md 10 / lg 14`；
  `--shadow-sm: 0 1px 3px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.10)`；
  `--shadow-lg: 0 16px 44px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.14), 0 0 0 0.5px rgba(255,255,255,0.10)`
  （顶缘内高光即"光打在玻璃上沿"，靠 inset 阴影，不新增令牌）
- 终端：`--bg-terminal: #0a0c11`（保持深底，ANSI 约定）

渐变覆盖：`[data-theme="glass"][data-gradient="on"] { --bg-elevated: rgba(255,255,255,0.06); --bg-input: rgba(255,255,255,0.04); }`

### 浅墨（浅底 · `data-theme="glass-light"`）

- 层级：`--bg-base: #eef1f7`（浅兜底）；`--canvas-bg: transparent`
- 面板：`--bg-elevated: rgba(255,255,255,0.55)`；`--bg-elevated-solid: #ffffff`；
  `--bg-input: rgba(255,255,255,0.45)`；`--bg-input-solid: #f5f7fb`
- 交互：`--bg-hover: rgba(20,25,45,0.06)`；`--bg-active: rgba(20,25,45,0.10)`
- 分隔/描边：`--divider-primary: rgba(20,25,45,0.10)`；`--border-default: rgba(255,255,255,0.70)`；
  `--border-subtle: rgba(20,25,45,0.12)`；`--border-input: rgba(20,25,45,0.16)`；`--bg-slider: rgba(20,25,45,0.14)`
- 文字（墨）：`--text-primary: #1d1d1f`；`--text-secondary: rgba(20,22,35,0.62)`；`--text-tertiary: rgba(20,22,35,0.48)`
- 强调色（加深保浅底对比，≥AA）：cyan `#0891b2` / purple `#9333ea` / orange `#d97706` / green `#16a34a` /
  red `#dc2626` / yellow `#ca8a04`；indigo `#4f46e5`；pink `#db2777`
- status-*：深字 + 浅透明底（对比达线，避免"白底白字"）
- 圆角/阴影：与玻璃同 `6/10/14`；
  `--shadow-sm: 0 1px 3px rgba(60,70,110,0.14), inset 0 1px 0 rgba(255,255,255,0.80)`；
  `--shadow-lg: 0 14px 40px rgba(60,70,110,0.22), inset 0 1px 0 rgba(255,255,255,0.90), 0 0 0 0.5px rgba(255,255,255,0.60)`
- 终端：`--bg-terminal: #14161c`（**保持深底** —— 浅底上 ANSI 前景不可读，与手记同款取舍：界面浅、终端深）

渐变覆盖：`[data-theme="glass-light"][data-gradient="on"] { --bg-elevated: rgba(255,255,255,0.50); --bg-input: rgba(255,255,255,0.40); }`

## 玻璃折射层设计（global.css）

保持现有"分档施加"策略，选择器列表不变（settings-dialog / confirm-dialog / modal-dialog /
modal / bulk-modal / dialog / panel / titlebar 为大浮层；`.hcc` 为中卡片；小控件/终端/编辑器/表格
不加 blur），仅按主题拆两套参数：

- 大浮层：玻璃 `blur(30px) saturate(160%)`；浅墨 `blur(30px) saturate(180%)`
- 中卡片 `.hcc`：玻璃 `blur(24px) saturate(150%)`；浅墨 `blur(24px) saturate(170%)`

（现有实现是暗 glass 单套 50/200 + 40/180；本次改为 macOS 克制的 30/24 档，并对两主题分别给参
数。`-webkit-backdrop-filter` 前缀保留。）

## 注册与联动校验

- `useAppearance.applyTheme`：通用 else 分支对 `glass-light` 自动 `setAttribute('data-theme','glass-light')`
  并 `applyMonacoTheme('glass-light')`，无需改分支逻辑；只需补 `monaco-setup.THEME_MAP`（见改动范围）。
- 三个穷举点（`Theme` 类型 / `THEMES` 数组 / `themeLabels` Record）与 `THEME_MAP` 必须同步，漏一处即
  编译期或运行期暴露。

## 测试与验证

1. `npx vue-tsc --noEmit`（或既有 typecheck 脚本）：确认四个穷举表补齐、无遗留 `glass` 缺档错误。
2. `npx vitest run src/styles/variables.test.ts`：确认主题块**只含令牌、无嵌套规则**、无孤儿 `:root` 令牌、
   结构画布仍读 `--canvas-bg`、终端仍读 `--bg-terminal` 全绿。
3. `npx vitest run src/stores/settings.test.ts`：`asEnum` 对 `glass-light` 放行、脏值兜底不误伤。
4. 手动矩阵：`{玻璃, 浅墨} × {渐变关, 渐变开(各形态/强度), 自定义背景图}`，逐项看
   面板折射、文字对比、终端/编辑器衔接、标题栏与侧栏透出。
5. 回归其余 8 个主题不受影响（只加/改 glass* 块，未动 `:root` 默认与通用规则）。

## 风险与回退

- 风险点：`--canvas-bg: transparent` 使结构画布透出 body 渐变，若用户把渐变调到极端亮/花，
  白色文字（玻璃）或墨字（浅墨）在个别区域对比可能吃紧 —— 由半透明面板 + 顶缘高光 + 适度
  blur 兜底；手动矩阵第 4 项专门复核。若某档不合格，微调该主题 `--bg-elevated` α 即可，不回退机制。
- 回退：改动集中在 6 个文件的样式/注册层，`git revert` 单个提交即可整体回退。

## 实施计划产出

本 spec 通过后，转入 writing-plans 生成可执行分步实施计划（令牌块重写 → 注册点 → 折射层 → 校验）。

---

## 实施回填（Implementation Backfill）

实际落地与本设计初稿的差异与延伸，均经浏览器 computed-style 取证（glass / glass-light × 渐变 on/off 切换）。

### 1. 根节点背景：回退式 → 叠层式（关键修正）
本设计初稿沿用 `body { background: var(--theme-gradient, var(--bg-base)) }`（回退式）。实装发现：glow/radial 渐变中段含
`transparent` 断点（旧机制靠“内容不透明”兜底），而玻璃把整页画布改为透明后，渐变透明处会露出**窗口 backgroundColor（深黑）**，
浅墨出现“亮蓝→黑带→浅底”割裂。改为**叠层式**：`background-color: var(--bg-base)` + `background-image: var(--theme-gradient, none)`，
透明处始终露出主题自身 `--bg-base`，不再依赖窗口底色。此修正对全部 10 主题无回归（内容不透明时渐变仍盖住打底色）。

### 2. 背景统一模型（用户确认的全透壁纸模型）
目标：一主题内所有背景为同一层，由「主题渐变色」开关整体调制。落地三要点：
- **全部非卡片结构面走 `--canvas-bg`**（玻璃下 = transparent，其余主题下 = `var(--bg-base)`，零回归）：
  8 个整页视图根（Dashboard / Audit / Schedules / Docker / Tunnel / RemoteManager / SettingsDialog `.settings-nav`）
  + 各类结构工具条/表头/空态（`.dash-body`、`.sftp-session-bar`、`.pane-header`/`.breadcrumb`/`.sftp-pane`、`.rm-footer` 等）。
- **卡片保持半透 + blur**：`--bg-elevated`。
- **唯二例外：sticky 表头与浮层对话框**（其下有内容滚动 / 是浮层），在 `global.css` 玻璃块用 `:root[data-theme=…]` 高特异度（(0,3,*) 压过 scoped (0,2,1)）
  覆盖为 `--bg-elevated` + `blur(20)` 磨砂：`.audit-table th`、`.ptable th`、`.rhv-table th`、`.rm-row-header`、`.file-list-head`、`.dt-dialog`。
  组件自身保持 `--bg-base` 不动 → 非玻璃主题零回归。

### 3. 浅底翻牌列外扩展（全仓排查发现）
`GpuNvtopPanel.vue` 的浅底翻牌（亮绿卡名/白网格在浅底不可见）原仅挂 `[data-theme="light"]`，导致浅墨（及 atelier）浅底下 GPU 面板不可读。
扩展为 `light / atelier / glass-light` 三档共用。

### 4. 有意保留的不透明面（非冗余，为可用性与既定约定）
- 终端 `.term-host`（xterm.js 不支持透明）、Monaco 编辑器、`.nvtop` 画布本底：均为深色/专用背景，按既有约定保持。
- 输入控件 `--bg-input`、小控件：不加 blur（实时输入区 blur 性能代价）。
- `HistoryPanel` 遮罩、`CU_WORKSPACE`/`SSH_TERMINAL` 导航背景：跨主题硬编码深底，属“关联对”守卫范畴，不动。

### 5. 未纳入本次提交
- `SftpView.vue` 的 2 处结构面迁移（`.sftp-view` 根 / `.sftp-session-bar`）因该文件同时处于并行 SFTP 传输中心重构中（数百行改动），
  为避免将他人 WIP 一并提交，本次提交**排除** `SftpView.vue`；该 2 处改动保留在工作区，待并行重构入仓后由其版本携带 `--canvas-bg`。

### 6. 校验结果
- `vue-tsc --noEmit`：零错误。`variables.test.ts`：22/23（唯一失败为并行未跟踪 `GithubAccelView` 的 `var(--font-mono, monospace)` 回落到字族守卫，与本改动无关）。
- 浏览器 computed-style 取证：各视图容器 + `.app/.titlebar/.ssh-view` 均 `rgba(0,0,0,0)`；ON↔OFF 整窗同步切换无分裂；
  `.audit-table th` / `.file-list-head` / `.settings-dialog` 实测半透 + blur；除 body 外全页无不透明白/黑大面。
