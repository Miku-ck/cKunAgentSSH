# 同步功能重构设计：GitCode 唯一主仓 + 平台镜像

- 日期：2026-10-30
- 状态：待评审
- 决策模型：GitCode 唯一主仓 + 平台镜像（应用只推 GitCode，GitHub/Gitee 由 GitCode 内置镜像自动搬运）

## 1. 背景与目标

当前 `GitView` 三个子页：`加速` / `仓库` / `同步`。

- 同步页（`src/components/ssh/git/GitSyncPanel.vue`）实现为「主仓 + 只读镜像」一致性卡，动作含补齐(快进推)、拉取合并(`--ff-only`)、切主仓，并自动探查三端。
- 仓库页（`GitReposPanel.vue` / `GitRepoDetailPanel.vue`）的推送目标直接用 `row.upstream`（通常是 github），完全未接入 GitCode 主仓概念。

问题：

1. 同步页功能上仍「推三端 + 逐端巡检」，与已写的引导文案（GitCode 主仓 + 平台镜像）不一致。
2. 仓库页一点「推送」就可能直推 GitHub/Gitee，绕过平台镜像，与模型冲突。
3. 两个模块缺统一主仓判据，推送逻辑各写各的、会打架。

目标：把**「仓库」与「同步」两个模块**统一到 GitCode 唯一主仓 + 平台镜像模型，消除推送逻辑冲突。

## 2. 范围

- **IN**：仓库模块、同步模块、共享核心 `shared/git-repos-core.ts`、主进程 `electron/git-repos.ts` 相关 IPC。
- **OUT**：加速子页（GitHub 加速）保持不动；「已加速」徽章仍是 `github.com`。"gitcode 为主仓库"只作用于仓库/同步两模块的推送与巡检语义。

## 3. 共享主仓判据（shared/git-repos-core.ts）

- 改造 `deriveSyncConfig(repoPath, branch, remotes, saved?)`：主仓 = host 命中 `gitcode.com` / `gitcode.net` 的 remote（若存在），否则回退 `branch.<b>.remote` > `origin` > 首名。
- 导出 `primaryRemoteOf(remotes, branchRemote): string | null`，供仓库、同步两模块共用。
- 镜像 = 其余 remote。

## 4. 仓库模块改动

文件：`GitReposPanel.vue`、`GitRepoDetailPanel.vue`，及主进程 `pushOne`。

### 4.1 推送目标改为主仓
- `GitReposPanel.pushTarget()` 与详情面板「推送目标」预览，改为显示主仓(gitcode) 而非 `upstream`。
- 主进程 `pushOne(path)`：解析主仓 = `primaryRemoteOf(...)`；有上游且上游 remote == 主仓 → `git push`；否则 `git push -u <主仓> <branch>`。
- 若仓库无 gitcode remote：`pushOne` 回退到 `upstream`（保持现状可用），并在渲染层提示「未配置 GitCode 主仓，本仓不走平台镜像」。

### 4.2 主仓/镜像标识
- 列表项（左列）与详情面板头部增加「主仓」章（primary = gitcode 的仓库）。
- 详情面板「远端」列表中，镜像 remote 标「只读镜像」，仅为信息标注、避免误解（仓库模块本就不推镜像）。

### 4.3 不变
- `fetch` / `pull`（`git pull --ff-only`）保持现状。
- PAT 已支持 gitcode（`PROVIDERS` 首项即 gitcode），无需改动。

## 5. 同步模块改动（方案 B 布局）

文件：`GitSyncPanel.vue`（与现有结构同源，重构为只读巡检）。

### 5.1 布局（方案 B：三端对照卡）
- 左：仓库分组列表（需要同步 / 已一致 / 未纳入），与现状一致。
- 右：三张对照卡纵向排列：
  - **GitCode 主仓卡（可写）**：显示 一致 / 已落后 / 已领先；动作 `[推送到主仓]`（本地领先时）/ `[从主仓拉取]`（本地落后时，`--ff-only`）；一致时无按钮。
  - **GitHub 镜像卡（只读）**：显示 同步 / 落后(待镜像) / 探测失败。无动作按钮。
  - **Gitee 镜像卡（只读）**：同上。
- 底部状态栏：一致 / 落后 / 分叉 / 镜像待同步 计数 + 巡检时间 + 自动频率。

### 5.2 工具栏
- 保留 `[巡检]`（ls-remote 三端 + rev-list 分型）。
- 新增 `[全部推送到主仓]`：对全部仓库的 gitcode 主仓执行推（复用 `syncCatchup(remotes=[主仓])` 或 `pushOne`）。
- 自动频率保留（默认 5 分钟）。

### 5.3 移除切主仓
- 同步页移除「切主仓」入口。
- 移除 `gitreposSyncSetPrimary` IPC 注册 + 实现 + store 调用 + 单测（主仓自动且唯一，不再需要手动设）。
  - 若清理成本过高，可保留 IPC 但不再暴露 UI；优先彻底移除。

### 5.4 边界态
- 仓库无 gitcode remote：该仓库在同步页显示只读巡检 + 黄色提示「未配置 GitCode 主仓，请在仓库子页登记 gitcode 远端」。
- 镜像非预期分叉（`syncProbe` 分类出 `diverged`，即镜像端存在本地没有的提交，可能有人直推 GitHub/Gitee）：红色告警，提示「请用 git fetch gitcode 后人工核对」（平台镜像为单向，不应出现）。

### 5.5 镜像「落后」语义
- 应用推完 GitCode 后，GitHub/Gitee 在平台镜像追平前会短暂「落后」——按「落后(待镜像)」中性提示，非错误/告警。

## 6. 主进程改动（electron/git-repos.ts）

- `pushOne(path)`：接入主仓判定，推 gitcode（见 4.1）。
- `syncCatchup(path, remotes?)`：保留通用实现；同步页调用时只传主仓（镜像不再被推）。
- 移除 `syncSetPrimary` 实现 + IPC 注册 + 测试。

## 7. 验证

### 7.1 单测
- `deriveSyncConfig` / `primaryRemoteOf`：gitcode host 优先；无 gitcode 时回退 origin / 首个。
- `pushOne`：推主仓（mock `execGit` 校验命令含 `gitcode`）。
- `syncProbe`：四态(ok / behind / diverged / unknown)分类正确。
- 移除 `syncSetPrimary` 后相关测试清理。

### 7.2 手动
- 本仓库（gitcode 已在 remote）登记到仓库子页后：
  - 仓库页「推送目标」应显示 `master → gitcode/master`，实际推送应上 gitcode；
  - 同步页显示 GitCode 主仓卡（可写）+ GitHub/Gitee 镜像卡（只读）；
  - 推送主仓后，镜像卡短暂「落后(待镜像)」再恢复「同步」。

## 8. 实施顺序（建议）

1. 共享核心：`primaryRemoteOf` + `deriveSyncConfig` 改造 + 单测。
2. 主进程：`pushOne` 主仓化；`syncCatchup` 调用收敛；移除 `syncSetPrimary`。
3. 同步页（方案 B）重构 + 移除切主仓 + 边界态。
4. 仓库页：推送目标改主仓 + 主仓/镜像标识 + 提示。
5. 手动验证 + 回归。

## 9. 已确认决策

1. 镜像「落后」= 待平台镜像（中性，非错误）。
2. 无 gitcode 仓库 = 只读巡检 + 黄色提示（不隐藏）。
3. 加速模块本次不动。
4. 加「全部推送到主仓」一键动作。
