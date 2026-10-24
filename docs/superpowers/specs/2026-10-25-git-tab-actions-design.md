# Git 工具页 V2：分支切换 / 提交 / 推送 + 术语与表格对齐

日期：2026-10-25。前置：2026-10-24-git-tool-tab-design.md（V1 只读 + fetch/pull --ff-only）。
本 spec 把 V1 刻意排除的三个写操作（switch / commit / push）以受控形态补进详情弹窗，
并统一 UI 术语为 git 专业名词、修表格列对齐、提交图谱默认折叠。

范围默认（澄清未响应，按推荐执行）：提交粒度 = 按文件勾选暂存后提交（VS Code 式）；
覆盖 = 本地 + 远程（远程写操作走确认框 + 审计链，与远程 pull 同口径）。

## 1. 术语映射（UI 文案 → git 专业名词）

| 现文案 | 改为 | 依据 |
|---|---|---|
| 脏区 N 处 | 未提交变更 N | dirty working tree / uncommitted changes |
| 分歧 ↑a↓b | 已分叉 ↑a↓b | diverged branches |
| 无远端 | 无远程仓库 | remote |
| 未设上游 | 未设置上游分支 | upstream |
| HEAD（游离） | (HEAD 游离) | detached HEAD |
| fetch（按钮） | 获取 | fetch |
| 拉取 / 推送 | 不变 / 新增 | pull / push |
| 取数失败 | 读取失败 | 应用措辞，非 git 术语 |

表格列头「远端」→「远程」；详情概要「远端」→「远程仓库」。

## 2. 切换分支（git switch）

- 入口：详情弹窗分支列表每行「切换」按钮（当前分支除外）。
- 命令：`git -C <path> switch <branch>`（DWIM：仅有远程跟踪引用的分支会自动建本地分支）。
  远程命令：`git -C <sq path> switch <sq branch>`。
- 校验：新核心函数 `isValidBranchName`——拒绝空 / 前导 `-` / 空白 / 引号 / 控制字符 /
  `~^:?*[\` / `..` / `@{` / `.lock` 结尾，长度 ≤ 256。命令面双保险（转义 + 前置校验）。
- 安全：不做 stash、不丢弃变更——工作区变更与目标分支冲突时 git 自己拒绝，
  stderr 首行原样上浮；远程切换 = 部署语义，确认框（danger）+ 审计。
- 分支名来自 `parseBranchVv` 的本地分支列表，不经用户手输（V2 无新建分支）。

## 3. 提交（git add + git commit）

- 入口：详情弹窗「工作区变更」清单——每行勾选框（默认全选）+ 提交信息输入 +
  「提交」按钮（无勾选或信息为空时禁用）。
- 语义 = 「提交所选」：先 `git add -- <选中路径…>`（覆盖未跟踪文件），
  再 `git commit -m <msg> -- <选中路径…>`。`commit -- pathspec` 只提交所选路径的
  当前工作区内容，忽略索引里既有但未勾选的暂存——与 VS Code 勾选提交同语义。
- 服务层单 IPC `gitrepos:commit(path, files, msg)` 内部串行 add→commit（两步 execFile
  数组，不经 shell）；远程为一条复合命令（add `;` commit，路径与信息一律 shSingleQuote）。
- 提交信息：单行输入，去首尾空白，≤ 200 字符，控制字符拒绝。
- 结果解释 `interpretCommit`：退出 0 → 从输出提取短哈希与分支（`[main abc1234] msg`）；
  失败 → stderr 首行（如「nothing to commit」原样上浮）。
- 确认：本地不确认（可 amend 撤销）；远程确认（danger）+ 审计。

## 4. 推送（git push）

- 命令：有上游 → `git push`；无上游但已知远程名与分支 → `git push -u <remote> <branch>`
  （建立跟踪，专业默认）；HEAD 游离或无远程 → 按钮禁用并提示。
- 远程名来源：`pickRemote` 重构为返回 `{ name, url }`（挑选规则不变），行数据新增
  `remoteName`；远程行从 `parseBranchVv` 的 upstream 与 remote 配置同法推导。
- 结果解释 `interpretPush`：退出 0 → 成功（含 -u 建跟踪）；输出含 `rejected` /
  `fetch first` → 提示「远端有你没有的提交：先拉取（pull）合并后再推送」；其他 → stderr 首行。
- 确认：本地与远程都确认（push 影响共享远端）；远程 danger + 审计。

## 5. 提交图谱默认折叠

- 图谱小节头改为可切换（▸ 折叠 / ▾ 展开），默认折叠；折叠时不发起 graphLog 取数
  （懒加载，首次展开才取），减少每次打开详情的一次 git 调用。「加载更多」逻辑不变。

## 6. 列与列对齐

- 仓库表格改 `table-layout: fixed` + colgroup 显式列宽（仓库弹性、分支/状态/远程/
  最近提交/操作定宽），数据格 nowrap + 溢出省略——列宽不随内容/刷新漂移，表头与
  数据列严格对齐。

## 7. 动作链路与安全边界（沿用 V1 骨架）

- UI 动作一律由详情弹窗 emit 回 GitReposPanel：busy 锁（`switch:/commit:/push:<id>`）、
  确认框、远程审计（`AUDIT_ACTION_GIT`）、toast、refresh 都收在父组件；
  弹窗 watch 行数据的 branch / dirtyFiles / lastCommit.hash 变化自动重取详情与图谱。
- 本地执行仍 execFile 参数数组不经 shell；远程路径 / 分支名 / 提交信息 / 文件路径
  一律 shSingleQuote，外加登记 / 校验层前置拒绝。
- V2 仍不做：stash、reset、clean、强推（--force）、删除分支、新建分支、解决冲突。

## 8. 新增 IPC 与数据结构

| 通道 | 参数 | 返回 |
|---|---|---|
| `gitrepos:switchBranch` | path, branch | GitRawResult |
| `gitrepos:commit` | path, files[], msg | GitRawResult |
| `gitrepos:push` | path | GitRawResult |

`RepoRow` / `LocalRepoStatusResult` 新增 `remoteName`（展示远端的名字，可为 null）。

## 9. 测试

- core：`isValidBranchName` 拒绝面 / `pickRemote` 返回名字 / 三个远程命令构造（含引号
  转义）/ `interpretCommit` / `interpretPush` / 行组装带 `remoteName`。
- service（真实 git 仓库）：switch 后 status 分支变化、非法分支名拒绝、
  commit 只提交所选文件（另一文件仍脏）、无上游 push 友好失败。
- 回归：typecheck + 全量 vitest。
