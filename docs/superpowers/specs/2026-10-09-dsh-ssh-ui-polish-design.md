# dsh-ssh 插件 UI 优化设计（方向 A：克制动效微调）

日期：2026-10-09
状态：已获用户批准（范围、主题约束、方向 A、打磨清单全量、方案 1）

## 背景与目标

`dsh-ssh` 插件的“SSH 运维”面板功能完整（主机列表、PTY 终端、SFTP 文件、隧道管理、主机表单），
但视觉细节粗糙：emoji 图标、裸文字状态、间距与层次不统一、无过渡动效。
本次优化目标：不改变布局与功能，只在现有结构内打磨视觉细节与交互反馈。

## 已确认的约束

- 视觉范围：整体视觉风格、主机列表/侧边栏、终端区域、文件/隧道/表单弹窗（交互反馈不单独处理）
- 主题：继续通过 dsw alias 主题 token（`var(--dsw-alias-*, 兜底值)`）跟随宿主深浅色，不引入固定色
- 方向：A · 克制动效微调（保留现有布局和信息结构）
- 实现方案：方案 1 · 纯 CSS 打磨 + 最小 TSX 改动

## 改动范围

| 文件 | 改动 |
|---|---|
| `dsh-ssh/src/client/css.ts` | 主要改动：间距、层次、过渡、徽标、状态点、表格、弹窗 |
| `dsh-ssh/src/client/panel/SshPanel.tsx` | emoji（⇊、⋯、⌨）换成内联 SVG；其余不动 |
| `dsh-ssh/src/client/panel/TerminalTab.tsx` | xterm theme（行高 + 16 色 ANSI 调色板）；结束提示条加关闭按钮 |
| `dsh-ssh/src/client/panel/FilesTab.tsx` | 📁/📄 换 SVG 图标 |
| `dsh-ssh/src/client/panel/TunnelsTab.tsx` | 状态文字改成小徽标（已建立/关闭中/已关闭） |
| `dsh-ssh/src/client/panel/HostFormDialog.tsx` | 指纹框加复制按钮 |

明确不做（YAGNI）：

- 不碰 `api.ts`、协议（`protocol.ts`）、路由、后端
- 不改布局结构：侧栏宽度 250px、左栏 + 主区 + 底部状态栏、子标签形式保持现状
- 不新增功能：无拖拽上传、多选、目录树、历史命令等
- 不做 i18n 改动（文案保持中文）

## 各区域视觉决策

### 侧边栏（主机列表）

- 主机行选中态：保持底色（`--dsw-alias-bg-overlay`）+ 左侧 2px 品牌色竖条
- hover/选中加 100ms 过渡（background-color / border-color）
- “连接中”状态点加轻微呼吸动画（1s，低透明度脉冲）；“生产”主机保留红点
- 分组标题后追加主机数，形如 `PROD · 3`
- 搜索框 focus：品牌色描边 + 浅阴影

### 子标签区（主区顶部）

- 保持现有“下划线式”激活样式（inset 顶部 2px 品牌色），不改形式
- 字体与间距随全局微调

### 终端（xterm.js）

- `lineHeight` 1.2（默认）→ 1.3；字号保持 13；字体族保持
  `Cascadia Mono, Consolas, "Courier New", monospace`
- 16 色 ANSI 调色板对齐现有 token 色：
  成功 `#3fb950` / 警告 `#d29922` / 错误 `#f85149` / 品牌 `#4c82f7`，
  中性色采用 GitHub Dark 系灰阶（foreground 保持 `#e6e8ee`，背景保持 transparent，光标保持 `#4c82f7`）
- “会话结束”提示条：左侧加信息图标，右侧加 × 按钮可手动关闭
  （仅清前端 state，不影响 xterm；重新进入标签页行为不变）

### 文件 / 隧道表格

- 表头 sticky（`position: sticky; top: 0`，带背景色，滚动时固定）
- 行高 +1px（td padding 5px → 6px）
- 📁/📄 换成 14px 细线 SVG 图标：文件夹品牌色、文件次级色
- 空状态（无文件 / 当前主机没有隧道）居中显示，带浅色图标
- 隧道状态文字改为现有 badge 样式：已建立（成功色）、关闭中（警告色）、已关闭（中性）

### 表单弹窗（HostFormDialog）

- 表单行间距统一 12px，label 宽度 84px → 72px
- 指纹框（`.ckun-ssh-fingerprint`）：mono 等宽保持，右上角复制图标按钮
  （`navigator.clipboard.writeText`，失败时静默降级；复制成功后按钮短暂变为“已复制”）
- 弹窗打开：120ms fade + 轻微 scale（0.98 → 1）；遮罩保持现状

## 动效总则

只允许三类过渡，且均 ≤120ms：

1. `background-color / border-color / color` —— 主机行、按钮、标签
2. 弹窗开合 fade + scale（120ms）
3. connecting 状态点呼吸（1s 循环，唯一例外，非交互过渡）

不加其他动画。

## 验证

1. 运行 `dsh-ssh` 现有测试：`test/client.test.mjs`、`routes.test.mjs`、`security.test.mjs`、`vault.test.mjs`，确认无回归
2. 运行 `dsh-ssh/build.mjs`，确认客户端 bundle 构建成功
3. 手动冒烟：宿主 GUI 打开 SSH 面板，依次过 主机列表（选中/搜索/分组计数）→
   终端（ANSI 色、结束提示条关闭）→ 文件（图标、sticky 表头、下载）→
   隧道（徽标、建/关）→ 编辑弹窗（指纹复制、开合动效）

## 风险与回退

- 改动集中在 6 个前端文件，全部为 UI 层；`git revert` 单个提交即可整体回退
- sticky 表头在个别滚动容器下可能有兼容差异，冒烟时重点看文件标签滚动
