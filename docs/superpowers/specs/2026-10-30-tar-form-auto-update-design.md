# tar 形态自动更新 — 设计

> 状态：已确认并实施（2026-10-30；决策见 §6）
> 背景：交付形态是 `dist/ckunagentssh-<version>-win32-x64.tar.gz` 解包直发（无安装器、无 electron-updater）。
> 目标：让"装过一次的用户能升到新版本"，且不引入 NSIS/安装器。

## 1. 为什么不能用 electron-updater

`electron-updater` 的 Windows 路径绑死 NSIS/Squirrel（`app-update.yml` + 安装器 + `latest.yml` 签名清单）。本仓 2026-10-23 已把 nsis/portable 目标移除（见 `electron-builder.yml` 顶部注释），产物只有 `dir` + `tar.gz`。
强行接进来等于把发布策略回退到安装器时代，代价远大于自研一个「下载 tar.gz → 校验 → 换包 → 重启」。

## 2. 总体流程

```
主进程启动 ──(若存在 pending-update 标记且版本不同)──> 应用更新 ──> 重启
   │
   └─ 后台（首屏就绪后，非关键路径）: check()
          GET <feed>/latest.json ──> 版本比较 ──> 有新版本则记住 available
   用户点「立即更新」/ 自动下载
          GET <feed>/ckunagentssh-<ver>-win32-x64.tar.gz → 流式写盘（带进度）
          sha256 校验 ──> 解压到 staging（同卷）──> 写 pending 标记
   退出（before-quit 之后进程真的退出）
          外部助手（detached）等待 pid 消失 ──> 覆盖 appDir ──> 拉起新 exe
```

关键点：**覆盖必须发生在应用进程完全退出之后**。Windows 上运行中的 `cKunAgentSSH.exe` 自身不可替换，`app.asar` 也被 Electron 持有句柄。因此主进程只负责「准备好」和「写标记」，真正的换包交给一个 detached 助手进程。

## 3. 组件

### 3.1 `shared/update-core.ts`（纯函数，可单测）

| 函数 | 职责 |
|---|---|
| `compareVersions(a, b)` | 语义化版本比较，只认 `x.y.z[-pre]` |
| `pickRelease(manifest, {version, platform, arch})` | 从清单里挑出适用产物；platform/arch 不匹配返回 `null` |
| `isNewer(manifest, current)` | 版本 + 平台双重判定 |
| `stagingDirFor(appDir, version)` | `<appDir>/.update-staging-<version>`（**同卷**，保证后续 rename 不跨盘） |
| `swapPlan(appDir, stagingDir)` | 输出「旧目录改名 → staging 改名 → 删旧」三步 rename 脚本，供助手执行 |
| `formatBytes` / `progressPercent` | 下载进度展示 |

`swapPlan` 用 rename 而非 robocopy 覆盖：`A.exe` 所在目录整体改名在 Windows 上是允许的（锁在文件句柄上，不在目录项），而 `robocopy /MIR` 在覆盖失败时会留下半新半旧的目录，反而更难救。

### 3.2 `electron/updater.ts`（主进程）

- `checkForUpdates(): Promise<UpdateState>` — `https.get` 清单，10s 超时，失败静默（离线不打扰）。
- `downloadUpdate(release)` — 流式写 `<userData>/update/<ver>.tar.gz` + `sha256` 边写边算；完成后 `tar.exe -xzf` 到 staging（复用打包链路的 bsdtar 假设，Windows 10+ 自带）。
- `markPending(version)` / `readPending()` — 写 `<userData>/update/pending.json`（含 version、stagingDir、exeName、preparedAt）。
- `spawnApplier(plan)` — 写 `applier.ps1`（内容由 `buildApplierScript` 生成 + 等 pid 消失 + `Start-Process` 新 exe + 删备份），`spawn(..., {detached: true, stdio: 'ignore'}).unref()`。
  - ⚠️ 2026-10-30 实测推翻：Windows 的 `detached` 是 `DETACHED_PROCESS`（无控制台），`powershell.exe`/`cmd.exe` 拿不到控制台会**立即 exit 0 且什么都不做**（node 子进程不受影响）。生产必须走 `cmd /c start "" /min powershell.exe …` 由 `start` 另开控制台托管，cmd 随即退出、PowerShell 被孤儿化。这条缺陷只在 `electron/updater-e2e.test.ts`（真实 spawn）里可见。
- `cleanupStale()` — 启动时清理失败的 staging 残留与 `*.tar.gz`。
- `setFeed()` / `loadConfig()` — 设置页覆盖的清单地址（只收 http(s)）落 `userData/update/config.json`，优先于 `CKUN_UPDATE_FEED`。
- `download()` 前先做 appDir 写探针：装到 Program Files 之类的副本在下 171MB 之前就提示"需要管理员权限手动解压"。

状态机（推给渲染层）：`idle → checking → available → downloading(percent) → ready → applying`；任何一步失败进 `error`，但**不阻塞启动、不阻塞使用**。

### 3.3 IPC 与 UI

- `update:check`、`update:download`、`update:apply`、`update:setFeed`、`update:getState`，事件推送 `update:state`，走现有 `handleTrusted`/`webContents.send` 通道（与 `backend:status` 同形）。
- 设置页新增「关于与更新」分类（`SettingCategory` 增加 `'about'`）：当前版本、通道地址、检查、下载进度、重启并更新。
- 开发态（`app.isPackaged === false`）`appDir` 置空：`UpdateState.supported = false`，按钮禁用并说明原因（换的目录会是 `node_modules/electron/dist`）。

### 3.4 清单格式（`latest.json`）

```json
{
  "version": "0.2.0",
  "notes": "修复 xx；后端启动快 20%",
  "releases": [
    {
      "platform": "win32",
      "arch": "x64",
      "url": "https://.../ckunagentssh-0.2.0-win32-x64.tar.gz",
      "sha256": "…64 hex…",
      "size": 179000000
    }
  ]
}
```

`sha256` 必填：没有它则拒绝更新（`tar.gz` 无签名，哈希是唯一的完整性凭据）。

## 4. 失败与回滚

| 场景 | 行为 |
|---|---|
| 清单拉不到 / 超时 | 静默 `idle`，下次启动再试 |
| 哈希不匹配 | 删下载文件，`error`，保留旧版本不动 |
| 解压失败（磁盘满） | 清 staging，`error` |
| 助手覆盖到一半失败 | 旧目录已改名为 `.cKunAgentSSH-backup`；助手把 backup 改回原名，过程全部写 `<userData>/update/applier.log`（`swap done` / `cleanup done` / `rollback failed` 三档终态） |
| 备份删不掉（杀软/索引短暂锁） | 重试 3 次（间隔 500ms）；仍失败也不回滚，备份留到下次启动 |
| `appDir` 不可写（装在 Program Files） | 下载前预检（写探针），不可写则提示「当前目录需要管理员权限，请手动解压新版」，不进入下载 |

**回滚 = 重解压上一版 tar.gz**：目录是纯分发物，用户数据在 `userData`（`%APPDATA%\cKunAgentSSH`），换包不动数据层。这是选「目录模式」而不是「安装器」换来的最大好处。

## 5. 测试

- `shared/update-core.test.ts`：版本比较（含 prerelease）、平台/架构筛选、`buildApplierScript` 的三步顺序与回滚分支、staging 路径同卷断言、状态机迁移。
- `electron/updater.test.ts`：IO 编排（check/download/apply/consumePending/cleanupStale/setFeed/loadConfig），网络与进程全部注入。
- `electron/updater-e2e.test.ts`（Windows）：本地 HTTP 服务 + 真 tar.gz + 真 PowerShell 助手走完 check→download→apply→换包，是**唯一能发现那类"spawn 静默不执行"缺陷**的用例。
- 手动验收（无网络依赖）：本地起 `python -m http.server` 托管一个假 `latest.json` + 小 tar.gz，设 `CKUN_UPDATE_FEED` 指向它走全流程。

## 6. 已确认的决策

1. **更新落地方式**：原地覆盖 + 退出后助手（用户 2026-10-30 确认）。并发新目录会让"用户启动的是哪个版本"变不确定，只检查不安装则等于没做自更新。
2. **清单来源**：`CKUN_UPDATE_FEED` 环境变量为部署默认，设置页可覆盖（落 `userData/update/config.json`，覆盖值优先）；仓库不写死任何域名。

## 7. 明确不做

- 增量/差分升级（tar 全量 ~171 MB；差分需要自建 bsdiff 管线，收益不值）。
- 灰度/强制更新（无服务端下发能力）。
- 代码签名与证书校验（另立任务：未签名的包在 SmartScreen 上会被拦，更新通道不能替代签名）。
