# Git 工具 tab 设计（C 方案：加速 + 仓库管理双子页）

日期：2026-10-24 · 状态：已评审通过（原型三变体对比后选定变体一「分域列表」）

## 背景与决策

项目原有独立的「GitHub 加速」页（NavPage `github`，纯本地 Electron 功能）。
新增 git 仓库管理（本地 + 远程）时，评审结论：**未上线阶段把两功能合并为一个
「Git 工具」tab，Docker 式分段子页组织**——顶级 tab = 领域，子页 = 领域内工具；
避免顶栏按钮无限膨胀，也为后续 git 域功能（凭据、巡检、部署）预留同一屋檐。

仓库子页选**变体一「分域列表」**：位置切换（本地 / 每台已连接主机）+ 表格 +
详情弹窗，与 Docker 管理页交互心智完全一致；「统一总览」留作未来叠加模式。

## 结构

```
src/components/ssh/git/
├── GitView.vue            # 壳：page-head + 分段（加速|仓库），记忆上次子页
├── GitAccelPanel.vue      # 原 GithubAccelView 主体原样搬入（store/IPC/主进程零改动）
├── GitReposPanel.vue      # 仓库子页：位置切换 + 表格 + 状态栏 + 动作
├── GitRepoDetailDialog.vue# 仓库详情弹窗（自取数：工作区/分支/最近提交）
└── subTab.ts              # 子页选择的全局小状态（命令面板深链用，模块级 ref）
```

导航改名：NavPage `github` → `git`，TitleBar 按钮「GitHub」→「Git」
（git-branch 图标），巡检红点/`initHealth` 逻辑原样迁移，快捷键 Ctrl+Shift+7 不变。
六处清单同步（useSshNavigation / SshView / TitleBar / App / useGlobalShortcuts /
CommandPalette），`command-palette-pages.test.ts` 守卫对账。

命令面板：`git` 页拆两条深链条目（`打开：Git 加速` / `打开：Git 仓库管理`），
经 subTab.ts 先设子页再导航；两条目的 `page` 均为 `git`，守卫集合对账不变。

## 仓库子页功能（V1）

- **范围与注册**：位置 = 本地或某台已连接远程会话（连接 UI 只出现在仓库子页，
  不污染加速子页）。本地：目录选择器添加 + 指定根目录扫描发现 `.git`（深度≤3、
  上限 200 条）；远程：登记路径（经 `rev-parse` 校验），按 connectionId 记忆。
  注册表持久化 `userData/git-repos.json`（骨架照抄 github-accel：串行读改写 +
  tmp+rename 原子写 + 损坏隔离）。
- **状态总览**：列 = 仓库（名称+路径）、分支（↑↓）、状态徽标（脏区/分歧/落后/
  领先/干净/无远端）、远端（host + slug）、最近提交（时间·作者）；异常优先排序、
  名称/路径过滤；状态栏计数随当前范围走（对齐 Docker 惯例）。
- **仓库详情**（弹窗、自取数）：概要（分支/上游/工作区/远端）、工作区变更清单、
  分支列表（`branch -vv`）、最近提交（`log -20`）；底部 fetch / 拉取 / 打开终端。
- **操作（只读优先）**：
  - fetch：`git fetch --all --prune`（网络读，不写审计——与 Docker 读路径同口径）；
  - 拉取：`git pull --ff-only`——**分叉时失败而不自动合并**；本地确认框说明；
    远程仓库 pull = 部署语义，红字提示 + confirm + 审计（`git_repo_action` 动作走
    remoteAudit 通道）；
  - 打开终端：本地 → 新本地终端（`createLocalTerminal` 扩展 cwd）；远程 → 新
    SSH 会话 + 写入 `cd <path>`（复用 Docker doExec 模式），完成后 emit
    `exec-terminal` 激活终端；
  - 刷新：逐仓库取数汇总。
- **与加速联动**：origin 为 github.com 且 git insteadOf 生效（`health.activePrefix`）
  → 行内「已加速」标记；本地 fetch/pull 自动受益于全局 insteadOf；远程主机不受益
  （V2 可做远端 git 代理配置）。

## 执行通道

| 侧 | 通道 | 说明 |
|---|---|---|
| 本地仓库 | Electron IPC `gitrepos:*` → `electron/git-repos.ts` | execFile + `-C`，不经 shell |
| 远程仓库 | 渲染层构造命令（git-repos-core 纯函数）经 `sshExec` | 一仓一条复合命令（status/log/origin 以 `\x1e` 分段），读 20s / 动作 60-120s 预算 |
| 纯逻辑 | `shared/git-repos-core.ts` | 命令构造 / 输出解析 / 排序 / 拉取结果解释，单测覆盖；对照 docker-core.ts 定位 |

## 安全边界

- V1 无任何强制覆盖类操作（无 push/reset/clean/checkout）；pull 固定 `--ff-only`。
- 远程仓库删除仅从注册表移除，绝不触碰远端文件。
- 远程命令路径一律单引号转义（`shSingleQuote`）。
- FastAPI 后端不参与（与 Docker / 加速同路线）。

## V2+ 留位

定时 fetch 巡检 + 顶栏徽标扩展；push/reset 等写操作（安全评审后）；部署后动作
（拉取后重启服务）；多凭据身份；仓库内容浏览；统一总览模式（本地+远程混排）。
