# 启动关键路径提速 + tar-only 分发收敛 设计文档

日期：2026-10-23
状态：已确认（用户逐节批准）
范围：`electron/backend-manager.ts`、`electron/shortcut-win.ts`（新增）、`electron/utils/powershell-path.ts`（新增，自 backend-manager 提取）、`electron/utils/perf-mark.ts`（新增，审计打点转常驻）、`electron-builder.yml`、`scripts/package.js`、`.github/workflows/ci.yml` 及配套测试。

## 1. 背景与目标

用户目标：优化项目启动速度；发布策略收敛为**仅 tar 包**（解包目录直发，不再提供 nsis/portable 安装器）；tar 形态下桌面快捷方式图标与系统通知归属保持正确。

### 1.1 审计基线（2026-10-23 实测，dev 口径，`[perf]` 打点两轮采样）

| 阶段 | run1 | run2 |
|---|---|---|
| 进程启动 → main 模块加载完 | 119ms | 116ms |
| → `whenReady` resolved | 140ms | 138ms |
| → setup*IPC 注册完 | 145ms | 144ms |
| → `createWindow` 返回 | 165ms | 166ms |
| → 窗口 ready-to-show | 1192ms | 1103ms |
| → 后端孤儿枚举 await 完成（spawn 前） | 1460ms | 1254ms |
| → Python 解释器探测完成 | 1993ms | 1773ms |
| → spawn | 1999ms | 1780ms |
| → health ready（`backend:whenReady` 放行） | 2949ms | 2739ms |

组件级微基准：CIM 孤儿枚举 928–1246ms（powershell 冷启动为主）；`import fastapi, uvicorn` 探测 532ms；后端 standalone 冷启动 1098ms（热库）/1674ms（新库）。

结论：窗口链路健康（whenReady 前 ~140ms）；后端就绪链为**三段串行**（枚举 await → 解释器探测 → spawn+健康轮询），合计 ~2.6s，其中枚举与探测互不依赖。

### 1.2 验收口径

- `backend: spawned` ≤ 0.7s；`backend start resolved` ≤ 1.6s（dev 复测两轮均须达标）；
- `npm run package` 产出 `dist/*.tar.gz`（内含 win-unpacked 全量），CI 上传 tar 成功；
- tar 版首启后桌面出现带正确图标的快捷方式，开始菜单项携带与 `appId` 同值的 AppUserModelID。

## 2. 设计一：启动提速（backend-manager 就绪链去串行化）

### 2.1 A1 孤儿枚举移出 spawn 前阻塞（收益 ~1.1–1.3s）

- `start()` 仍在 spawn **之前**调用 `killOrphanBackends()`（枚举快照起点必须早于新进程存在，该语义不变）；
- 但 spawn 前**不再 `await`** 该 Promise；改为 spawn 之后 `await` 汇合，再 `killPids(orphans, [新pid, self])`；
- 误杀防护由 `killPids` 的 exclude 列表承担（已在位），杀灭延后 1–2s 无功能影响（孤儿是 server 端，无人拨号，持有过期 token 无害）；
- 端口冲突由 `findFreePort` 的真实 bind 探测天然规避（孤儿占用的端口自动跳过）；
- `killOrphanBackends`/`start()` 的时序契约注释改写为新口径：**枚举必须始于 spawn 前、汇合允许晚于 spawn，防误杀靠 exclude 而非 await**。

### 2.2 A2 Python 解释器：并行 + 乐观缓存（收益 ~0.5s）

- A1 后探测与枚举自然并行（免费）；
- 新增 userData 缓存 `backend-python.json`（`{cmd, prefixArgs, savedAt}`，沿用 desktop-prefs 的原子写 + 容错读模式）；
- 命中缓存时**关键路径零探测**：直接 spawn；
- 失效判定：健康就绪前任何启动失败（spawn ENOENT / exit 9009 / 健康超时）→ 删除缓存 → 走现有 `resolvePythonCommand()` 全量并行探测，经既有 auto-restart 链路重试（重启次数上限不变）；
- 打包（tar 解包）形态内置 `resources/python-runtime/`，`existsSync` 即中，缓存主要惠及 dev 与 `--allow-missing-runtime` 包。

### 2.3 错误处理

- 缓存文件损坏/脏值：`JSON.parse` 失败按无缓存处理并静默重建，绝不因缓存阻塞启动；
- 所有新逻辑失败路径与原实现同构（warn 日志 + 回退），不新增启动崩溃面。

### 2.4 测试

- 既有守卫测试中"先杀后起"时序契约改为"枚举先于 spawn、杀灭允许后到"；
- 新增：缓存命中路径零探测、启动失败后缓存失效并回退全量探测、损坏缓存按无缓存处理、exclude 防误杀（新 pid 出现在枚举结果中也不被杀）。

## 3. 设计二：tar-only 分发收敛

### 3.1 `electron-builder.yml`

- `win.target: [nsis, portable]` → `dir`；删除 `nsis:`、`portable:` 整块（含 `createDesktopShortcut: always`）；
- 保留 `appId: com.ckun.agent-ssh`、`productName`、`win.icon: resources/icon.png`、`win.executableName`、`files` 白名单（tar 体积=解包目录体积，白名单重要性不变）；
- L6-21 白名单教训注释保留，其中 makensis 段落按 tar 口径改写；
- `app-user-model-id.test.ts` 守卫测试不动（继续从 YAML 解析 `appId:` 行与 `APP_USER_MODEL_ID` 比对）。

### 3.2 `scripts/package.js` 追加 tar 步骤

- electron-builder 成功产出 `dist/win-unpacked/` 后，调用系统 `tar.exe`（Windows 10+ 自带；POSIX 用 `tar`）：`tar -czf dist/ckunagentssh-<version>-win-x64.tar.gz -C dist/win-unpacked .`；
- 内嵌 Python 运行时硬前置（ADR-0002）与 `--allow-missing-runtime` 逃生口均不变；
- tar 失败（如系统无 tar）→ 非零退出并给出明确报错，不出"半成品发布物"。

### 3.3 `.github/workflows/ci.yml`

- 步骤名 `Package (nsis + portable)` → `Package (tar.gz)`；
- 上传产物 `dist/*.exe` → `dist/*.tar.gz`，`if-no-files-found: error` 保留；
- L74 注释"交付目标是 nsis/portable"改写为 tar 口径。

### 3.4 决策记录

- 老 NSIS/portable 用户的升级路径随 target 移除而消失——用户明确拍板"仅 tar 包，不再提供其他格式"，本设计不再兼容。

## 4. 设计三：.lnk/AUMID 生成器（tar 形态补回安装器职责）

新模块 `electron/shortcut-win.ts`，仅 `win32 && app.isPackaged` 生效：

- **开始菜单项**（Toast 归属注册的关键）：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\cKunAgentSSH.lnk`，目标 `process.execPath`，写入 `AppUserModelID = APP_USER_MODEL_ID`（与快捷方式名共用同一常量来源）；每次启动 `existsSync` 检查、缺失即补；
- **桌面快捷方式**：仅首启创建一次（userData 标记文件控制，用户删除后不骚扰）；桌面路径由脚本内 `[Environment]::GetFolderPath('Desktop')` 解析，兼容 OneDrive 重定向；
- **AUMID 写入**：WScript.Shell 不支持该属性，用 `execFile` 执行 PowerShell `Add-Type` C# 片段（`SHGetPropertyStoreFromParsingName` + `IPropertyStore.SetValue(PKEY_AppUserModel_ID)`）；PowerShell 绝对路径解析逻辑从 `BackendManager.resolvePowerShell` 提取为共享 util（`electron/utils/powershell-path.ts`），backend-manager 改为引用；
- **时序**：`whenReady` 内、窗口出现之后异步触发（fire-and-forget），失败仅 `console.warn`，不阻塞启动；
- **图标**：.lnk 不指定 IconLocation，默认取 exe 内嵌图标（electron-builder 由 `resources/icon.png` 生成多尺寸 .ico，2026-10-23 已更换为猫形 C 新图标）。

通知归属闭环（tar 形态）：进程级 `setAppUserModelId(APP_USER_MODEL_ID)`（已有）+ 系统级开始菜单 .lnk 同值 AUMID（本节）→ Toast 横幅注册、任务栏分组、点击回跳全部与 NSIS 时代等效；两处同值由守卫测试 + 共用常量保证。

测试：路径构建、PS 脚本片段含 AUMID 常量拼接断言、非 win/非打包态跳过、幂等（已存在不重建）、桌面首启标记只建一次。

失败边界：受限语言模式（WDAC/ConstrainedLanguage）下 Add-Type 失败 → 仅"快捷方式没建出来"，warn 留痕，应用本体不受影响。

## 5. 审计打点的去留

`electron/utils/perf-mark.ts` 与 main/backend-manager 中的 `[perf]` 打点**保留为常驻轻量日志**：启动时数行输出，为 1.2 节验收口径提供证据、且是后续回归的观测手段；实现计划中不再视为临时代码。

## 6. 测试与验证计划

1. `npx vitest run electron/`（含改写后的孤儿时序测试、新增 shortcut-win 测试、既有 app-user-model-id 守卫）；
2. `npm run dev` 两轮，按 `[perf]` 打点核对 1.2 验收口径；
3. `npm run package -- --allow-missing-runtime`（本机快速验证 tar 出件与内容布局），确认 tar 内 `resources/icon.png`、`resources/backend/` 在位；
4. 解压 tar → 启动 `cKunAgentSSH.exe`：验证托盘图标、桌面/开始菜单快捷方式图标、开关"桌面通知"后 Toast 归属显示为 cKunAgentSSH。
