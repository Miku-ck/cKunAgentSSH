# 仓库远程可自由设为主仓

- 日期：2026-10-30
- 状态：待评审
- 关联：`2026-10-30-sync-gitcode-primary-design.md`（原「GitCode 唯一主仓」已改为可手动覆盖）、`2026-10-30-git-repos-sync-merge-design.md`（仓库/同步合并）

## 1. 背景与目标

原设计（sync-gitcode-primary）把主仓定为**自动且唯一**：GitCode host 命中即主仓，并移除了手动「切主仓」入口（`syncSetPrimary` 被改名成 `__SYNC_SETPRIMARY_REMOVED__`）。

改进点：仓库的远程应**可自由设置是否为「主仓」**——即手动优先、可回退自动。既要保留自动推导的便利，又要让用户能对任意远端（gitcode / github / gitee / 自建）显式指定为主仓并持久化。

已具备的骨架（直接复用，不新增存储结构）：
- `RepoSyncConfig { primary: string; mirrors: string[] }` 已定义；
- 注册表 `reg.sync?: Record<string, RepoSyncConfig>` 已能落盘（`sanitizeSyncConfigs` 已可读取）；
- 旧 `syncSetPrimary` 逻辑曾被写出但**未真正生效**（把选中名当 `branchRemote` 传入 `deriveSyncConfig`，GitCode 优先级仍会胜出）。

目标：把这套已埋好的骨架重新启用并修正，使「手动主仓」在判定、推送、巡检、UI 上全部生效。

## 2. 范围

- **IN**：共享核心判定、主进程 `pushOne` / `syncConfigOf` / `syncSetPrimary`、IPC 接线、详情面板「远端」页签 UI、列表行推送预览、单测。
- **OUT**：加速子页（GitHub 加速）不动；远程范围（SSH 会话）仓库的巡检本就不可用，主仓设置仅对本地仓库生效。

## 3. 模型与判定（shared/git-repos-core.ts）

### 3.1 新增 `resolveSyncConfig`
```ts
export function resolveSyncConfig(
  remotes: Record<string, string>,
  branchRemote: string | null,
  saved?: RepoSyncConfig | null,
): RepoSyncConfig {
  if (saved && saved.primary && remotes[saved.primary]) {
    return { primary: saved.primary, mirrors: Object.keys(remotes).filter(n => n !== saved.primary) }
  }
  return deriveSyncConfig(remotes, branchRemote) // 自动推导兜底
}
```

### 3.2 `primaryRemoteOf` 保持不变
仍作为「自动推导」的原子函数（GitCode host 优先 → `branch.<b>.remote` → `origin` → 字典序首名）。手动逻辑只叠加在 `resolveSyncConfig` 之上，不修改它。

### 3.3 手动优先 + 可回退
- `reg.sync[path]` 存在且 `primary` 仍是有效远端 → 用它作主仓；
- 否则回退 `deriveSyncConfig`；
- 手动主仓指向的远端被删除/改名 → `resolveSyncConfig` 检测不到，自动回退推导（不报错、不残留脏配置）；
- 「恢复自动」= 删除 `reg.sync[path]`。

## 4. 主进程改动（electron/git-repos.ts）

### 4.1 `syncConfigOf(repoPath, branch, remotes)`
原本忽略 `reg.sync`、直接 `deriveSyncConfig`。改为：先 `readRegistryFromDisk()` 取 `reg.sync[path]`，再 `resolveSyncConfig(remotes, branchRemote, saved)`。这样 `syncProbeOne` 产出的端点角色（主仓/镜像）随之反映手动设置。

### 4.2 `pushOne(path)`
原先用 `primaryRemoteOf(remotes, branchRemote)`（纯自动）。改为读 `reg.sync[path]` 后走 `resolveSyncConfig(...).primary`：
- 上游已指向手动主仓 → 直接 `push`；
- 否则 `push -u <主仓> <branch>` 建立跟踪；
- 无手动也无任何远端 → 原「没有配置远程仓库」报错不变。

### 4.3 重写 `syncSetPrimary`
替换废弃的 `__SYNC_SETPRIMARY_REMOVED__`：
```ts
async syncSetPrimary(path: string, primary: string | null): Promise<GitReposRegistry> {
  return this.enqueue(async () => {
    const repoPath = assertRepoPath(path)
    const reg = await readRegistryFromDisk()
    if (!primary) {                       // null / 空 = 恢复自动
      delete reg.sync?.[repoPath]
    } else {
      const name = String(primary).trim()
      if (!isValidBranchName(name)) throw new Error(`非法远端名: ${name || '(空)'}`)
      const remotesRaw = await execGit(['-C', repoPath, 'config', '--get-regexp', '^remote\\..*\\.url$'])
      const remotes = parseRemoteConfig(remotesRaw.stdout)
      if (!remotes[name]) throw new Error(`远端 ${name} 不存在（现有：${Object.keys(remotes).sort().join('、') || '无'}）`)
      reg.sync = reg.sync ?? {}
      reg.sync[repoPath] = { primary: name, mirrors: Object.keys(remotes).filter(n => n !== name) }
    }
    await writeRegistryToDisk(reg)
    return reg
  })
}
```
- `primary` 为 `null`/空 → 清掉该仓手动配置（恢复自动）；
- 非空 → 校验远端存在后写入，mirrors 由当前远端实时计算（被删远端自动掉出）。

## 5. IPC 接线

- `electron/preload.ts`：`gitreposSyncSetPrimary: (path, primary?: string | null) => ipcRenderer.invoke('gitrepos:syncSetPrimary', path, primary ?? null)`
- `electron/main.ts`：`handleTrusted('gitrepos:syncSetPrimary', (_e, path, primary) => gitRepos!.syncSetPrimary(path, primary))`

## 6. UI（GitRepoDetailPanel.vue「远端」页签）

- 每个远端行新增「设为主仓」按钮（当前已是主仓的行不显示，或以禁用态呈现）；
- 当前主仓（自动或手动）显示 `主仓` 徽标，其余 `镜像`；
- 该仓库存在手动主仓时，显示「恢复自动」按钮（调用 `setPrimary(null)`）；
- 新增 `emit('setPrimary', remote: string | null)` 回抛父级 `GitReposPanel` 执行 IPC；
- 成功后父级**重探该仓端点**（`gitrepos:syncProbe([path])`）使徽标与状态刷新，并刷新 registry 映射。

### 6.1 列表行推送预览
提交弹窗「推送目标」用 `repo.primaryRemote`。渲染层构建行时取 `gitreposGetRegistry()` 的 `sync` 映射，传给 `toLocalRepoRow(r, savedSync?)`（shared core 该签名新增可选第 2 参数），使预览与真实推送一致。

## 7. 边界与错误处理

- 手动主仓远端已不存在 → 自动回退推导，不报错；
- 多仓库各自独立设置，互不干扰；
- 远程范围仓库的「远端」页签不展示巡检，主仓设置仅对本地仓库生效；
- 校验远端存在、远端名合法，沿用既有 `isValidBranchName` / `parseRemoteConfig`。

## 8. 测试

- `resolveSyncConfig` 单测：手动覆盖 / 回退自动 / 手动远端已不存在→回退 / `null` 清除语义；
- `pushOne`：mock `execGit` 校验命令推到**手动主仓**（非 gitcode）；
- `syncConfigOf` / `syncProbeOne`：端点角色随 `reg.sync` 变化；
- 清理 `__SYNC_SETPRIMARY_REMOVED__` 遗留，相关旧测试移除或改写。

## 9. 实施顺序

1. 共享核心：`resolveSyncConfig` + `toLocalRepoRow` 加 `savedSync?` 参数 + 单测。
2. 主进程：`syncConfigOf` / `pushOne` 接 `resolveSyncConfig`；重写 `syncSetPrimary`；清理废弃占位。
3. IPC：`preload.ts` + `main.ts` 接线。
4. UI：详情面板「远端」页签加设主仓/恢复自动；父级执行 + 重探 + 刷新 registry 映射；列表行预览接 `savedSync`。
5. 手动验证 + 回归单测。

## 10. 已确认决策

1. 手动优先 + 可回退自动（默认仍自动推导，GitCode 优先逻辑保留）。
2. 仅本地仓库可设主仓；远程范围仓库不暴露该入口。
3. 每仓独立设置，无全局默认主仓。
4. 复用 `reg.sync` 存储，不新增结构。
