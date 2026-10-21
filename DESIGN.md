# DESIGN.md — 界面开发规范（精简版）

cKunAgentSSH 前端（`src/`）的视觉 / 布局 / 交互 / 组件约定。**描述已落地的规则，不是设想。**
改界面前先读这里；权威出处见文末「参考」。

---

## RULES — 硬规则（逐条自查）

**必须做**
- 颜色一律走令牌 `var(--*)`；透明色用 `color-mix(in srgb, var(--token) N%, transparent)` 派生。
- 结构背景用 `--bg-base`，卡片/弹窗/菜单/浮层用 `--bg-elevated`。**只有这两级**。
- 按钮用 `styles/button.css` 的 `.btn-*`；面板用 `settings-panel-base.css`；审批卡用 `approval-card.css`。
- 空状态双向居中（容器 `flex:1` + 双向 flex center），单行文字说清「这里为什么是空的」。
- 模态承载内容（`Teleport to body` + `Transition` + `role="dialog"`/`aria-modal` + Esc 关闭 + 卸载清理监听）。
- 文字对比度 ≥ 4.5:1；终端底一律深色 `--bg-terminal`。
- 破坏性操作（删除/清空/断开/危险命令）走 `ConfirmDialog` 二次确认。
- 焦点保留 `:focus-visible`（全局 2px 强调色 outline）。
- 新增/删除令牌后跑校验（见文末）。

**禁止做**
- 组件里写字面量颜色 `#hex` / `rgba()`（例外：平台约定色，如 Windows 关窗红 `#e81123`，须注释）。
- 新增背景层级（panel / surface / sidebar 等中间层）。
- 在组件里写 `if (theme === 'x')` 分支——装饰差异写进令牌（默认值 = 旧行为，只在目标主题块给实值）。
- 手写 accent→前景色表（用 `--accent-on`）；手写强调色 hex（用 `ACCENT_MAP`）。
- 在 `[data-theme]` 里写 `--accent-primary`（内联样式压过主题块，永不生效）。
- 在主题块里改尺寸令牌（`--titlebar-h` 等属版面）。
- 抽屉式交互（本项目偏好居中弹窗）。历史记录已改为居中弹窗（`HomeHistoryDialog`），不再保留任何抽屉例外。
- 空状态里重复头部已有的主操作按钮；列表容器与空状态拆成两个同级空壳 div（会平分 `flex:1` 把居中顶偏）。
- 新建/连接类主操作按钮配装饰图标（`+` / 箭头）——按钮只留文字。
- 独立写 scoped `.btn`、或给共享令牌写回落值。

---

## 令牌速查

位置：[src/styles/variables.css](src/styles/variables.css)（`:root` 深色默认，各主题块只覆盖差值）。

**背景**：`--bg-base` 结构底 · `--bg-elevated` 唯一浮起层 · `--bg-input` 控件凹陷底 · `--bg-slider` 滑块轨道 · `--bg-terminal` 终端内容底（恒深）
> `--bg-x` 与 `--bg-x-solid` 必须同值；`[data-gradient="on"]` 从 `-solid` 派生半透明（必须引用 `-solid`，否则自引用失效变透明）。

**文字**：`--text-primary` 正文 · `--text-secondary` 次要 · `--text-tertiary` 最弱（≥4.5:1，已按主题加深）
**边框**：`--border-default` 可见描边 · `--border-subtle` 弱分隔 · `--divider-primary` 分隔线 · `--border-input` 输入框
**形状**：`--radius-sm/md/lg` = 4/6/8px（手记 2/3/4px） · `--shadow-sm/lg` · `--transition-fast` = 150ms
**字体**：`--font-sans` 正文 · `--font-mono` 命令/代码 · `--line-height-normal` · `--font-display` 标题 · `--chat-font-size` 消息正文
**布局**：`--titlebar-h` = 32px · `--tabbar-h` = 34px
**强调色**：`--accent-primary` / `--accent-primary-rgb` 单一事实源 · `--accent-on` 压强调色底的前景 · `--accent-cyan` 命令行/代码 · `--accent-indigo*` 聊天强调 · `--accent-pink*` 目标 chip
**语义**：`--status-info/success/warn/error/debug-*`、`--status-tag-*` · `--card-perm-*`/`--card-warn-border` 权限卡 · `--danger-bg/-border/-text` 危险 · `--metric-cool/warm/hot` 监控（<60 绿 / 60–80 黄 / >80 红） · `--tint-l/r/d` 隧道徽标

**字号两套，别混**：界面基础字号 = `html { font-size }`（组件用 `rem` 跟随）；消息正文 = `--chat-font-size`。

---

## 主题系统

枚举 `Theme`（[appearance-tokens.ts](src/utils/appearance-tokens.ts)）：`dark`（默认，移除 `data-theme`）· `light` · `system`（运行时解析 dark/light）· `blue-screen` · `forest` · `mint` · `bee` · `coral` · `atelier`（手记）。
强调色 `Accent`：`blue/purple/green/orange/white/forest/mint/bee/coral/verm`，hex 表 = `ACCENT_MAP`。

- 2026-10 删除了「玻璃/晶白」（glass / glass-light）两档材质主题：持久化的旧键值由 `asEnum` 闸门回落 `dark`（由 settings.test.ts 钉住），折射层与主题块已从 global.css / variables.css 移除；历史设计文档（docs/superpowers/specs）按约定保留不动。

- 应用：根元素 `data-theme="<theme>"`；强调色由 `applyAccent()` 内联写 `--accent-primary/-rgb/--accent-on`。
- `--accent-on` 由 `contrastOnAccent()` 按 WCAG 相对亮度算（<3:1 换深字）。
- 动效开关 `[data-animation="false"]` + `prefers-reduced-motion` 已全局兜底——**新动效无需自己处理**。
- **新增主题**：`Theme` 加成员 → `variables.css` 加 `[data-theme="x"]` 块（覆盖背景/边框/文字/终端底/专用底；浅色系处理对比度与语义色转浅档）→ 需要色板入口则登记 `AppearancePanel`。不动任何组件。

---

## 布局

```
App.vue (flex column, 100vh)
├── 背景层 (可选 .app-background-layer / .app-overlay-layer，勿用 z-index:-1)
├── TitleBar          高度 --titlebar-h (32px)，-webkit-app-region: drag / 交互元素 no-drag
└── SshView           .main (flex:1, relative, z-index:1)
    ├── 标签栏 .term-tabs  高度 --tabbar-h (34px)
    ├── .content-area  多页面 v-show 切换 + pageMounted() 惰性挂载
    └── QuickCommandBar (可选，终端态才占这 30px)
```

- 首页恒在、不可关闭，不占标签；运行过程长在首页下方（勿做成标签页）。
- AI 只有一个界面：首页任务台。顶栏「AI」入口与 `Ctrl+Shift+B` 都是回到任务台，
  不再另有一块右侧面板（2026-10-20 退役，见 `docs/superpowers/specs/2026-09-30-retire-ai-side-panel-design.md`）。
- 设置弹窗：`overlay` `rgba(0,0,0,.6)` + `blur(2px)` `z-index:2000`；`dialog` `clamp(960px,90vw,1280px) × clamp(640px,88vh,860px)`，左导航 200px。确认框：宽 380px，`z-index:3000`。过渡 `opacity .15s` + 内容 `scale(.96~.97)`。
- 历史记录弹窗：`width: min(560px, 100vw - 48px)`、`max-height: min(640px, 100vh - 96px)`，居中；overlay 读 `--scrim`（随主题染色）+ `blur(2px)`，`z-index:2000`。

---

## 组件约定

**按钮** [button.css](src/styles/button.css)：底座 `.btn`（24px / `0 10px` / 4px / 400 字重 / 透明底）。
`.btn-primary` 业务主操作（强调色 + 50% alpha 描边）· `.btn-danger` 不可逆（`--danger-text` + 40% alpha）· `.btn-ghost` 更弱（次要色无描边）· `.btn-sm` 紧凑（20px）。
状态：hover `--bg-hover` / active `--bg-active` / disabled `opacity:.45`。**语义只用字色 + alpha 描边，禁止实心 CTA。**

**设置面板** [settings-panel-base.css](src/components/settings/panels/settings-panel-base.css)（`<style scoped src>` 引入）：`.panel` / `.panel-header`(h3 14/600 + `.panel-desc` 12 次要) / `.panel-body`(右 padding 4px) · `.setting-select`(28px) · `.retry-btn`。
**折叠分组** [settings-groups.css](src/components/settings/panels/settings-groups.css)：`.grp` / `.grp-head` / `.grp-caret`(展开转 90°) / `.grp-body` · `.hint`(`.err`/`.muted`) · `.link-btn` · `.flag`(`.ok`/`.vio`)。

**审批/权限卡** [approval-card.css](src/styles/approval-card.css)（`.ap-*`）：`.ap-card`(`--bg-elevated` + `--card-warn-border` + 10px) · `.ap-perm` 权限变体 · `.ap-code`(mono + `--accent-cyan`) · `.ap-risk.*` / `.ap-env.*` 徽章 · `.ap-actions` + `.ap-btn-{grant,always,session,deny}`（语义色是卡片独立的绿/青/灰，不随强调色）。宿主布局留 scoped，覆盖用「宿主类 + .ap-ic」复合选择器。

**图标**：内联 SVG，`stroke="currentColor"` + `fill="none"`（自动适配主题）。尺寸：标题栏/标签 14 · 设置导航 16 · 卡内 15 · 按钮内 14。复用图标放 `src/components/icons/`。Docker 入口与页面空态统一鲸鱼图形。

**标签状态点**：6px 圆点，活动 `--accent-primary` / 重连中 `--status-warn-text` 脉冲 / 断连放弃 `--accent-red`；判据单一来源 `src/utils/tabStatus.ts`（标签栏、溢出菜单、MRU 切换器三处共用）。

**目录**：`components/` 按域分 `common|ssh|agent|chat|home|settings|icons`；逻辑 `composables/`；状态 `stores/`（Pinia）。

---

## 交互

Esc 关闭/取消 · Enter 确认（仅确认框）· Tab 补全 · `shift+Enter` 换行（可配置）。
hover/active 用 `--bg-hover`/`--bg-active`，过渡 `var(--transition-fast)`；`::selection` 用 `--wash`；滚动条 6px（thumb `--border-default`，hover `--text-tertiary`）。
轻提示用 Toast，阻塞决策用确认框/审批卡。全局快捷键监听必须 `onUnmounted` 清理。

---

## 校验

命令清单只在 [README §命令](README.md#命令) 维护一份（本文件不复制，避免两边漂移）。改完样式/令牌最少要过
`npm run typecheck` + `npm test`：`variables.test.ts` 是令牌消费方守卫，弹窗误用半透明 `--bg-*`
而不是 `--bg-*-solid` 会被它直接判红。

## 参考

[variables.css](src/styles/variables.css) · [global.css](src/styles/global.css) · [button.css](src/styles/button.css) · [approval-card.css](src/styles/approval-card.css) · [useAppearance.ts](src/composables/useAppearance.ts) · [appearance-tokens.ts](src/utils/appearance-tokens.ts) · [App.vue](src/App.vue) · [TitleBar.vue](src/layouts/TitleBar.vue) · [SshView.vue](src/views/ssh/SshView.vue) · [SettingsDialog.vue](src/components/common/SettingsDialog.vue)