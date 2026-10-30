# 崩溃上报与诊断包导出 — 设计

> 状态：已确认并实施（2026-10-30）
> 背景：交付形态是解包直发的 `tar.gz`（无安装器，见 `2026-10-30-tar-form-auto-update-design.md`）；打包态是 Windows GUI 子系统，主进程没有 stdout/stderr。
> 目标：「用户说崩了」时，能拿到一份可读的现场（日志末段 + 崩溃转储 + 版本/平台/更新阶段），且这份现场**可以安全地发出来**。

## 1. 为什么需要

主进程的 JS 异常有 `uncaughtException` 兜底与 `main.log` 落盘，但两类事故完全不在那条链路上：

1. **原生崩溃**：渲染进程、GPU 进程、后端子进程被 OOM kill 或驱动问题打死时，进程直接消失，JS 层没有一行日志。现场只留在 Crashpad 的 `.dmp` 里。
2. **现场收集成本**：没有诊断包时，排障要一条条问用户「版本号多少、装的哪、日志在哪、能不能把那几个文件发我」。日志分散在 `userData/main.log`、`electron-bridge.log`、`Crashpad/` 里，用户既不知道在哪也不敢随便发。

因此拆成两件事：**崩溃上报（本地 Crashpad）**负责留证据，**诊断包导出**负责把证据打成一个文件、并保证里面没有凭据。

## 2. 组件

### 2.1 `shared/diagnostics-core.ts`（纯函数，无 IO）

| 函数 / 常量 | 职责 |
|---|---|
| `redactSecrets(text)` | 凭据抹除：`Bearer …`、`sk-…`、`ghp_…`、URL userinfo、`key=value` 一族 |
| `tailText(text, maxBytes)` | 只保留尾部 N 字节，按字节切后丢掉开头的替换符（不产生 U+FFFD） |
| `diagnosticsFileName(version, now)` | `ckunagentssh-diag-<ver>-<YYYYMMDD-HHmmss>.tar.gz`（文件名不含冒号） |
| `envSnapshot(env)` | 环境变量白名单 + 凭据键出 `***` |
| `buildSystemInfo(input)` | 组装 `system.json`（扁平：版本/平台/路径/运行时/更新/日志清单/崩溃数） |
| `DIAG_MAX_LOG_BYTES = 4MB` | 单份日志的尾部上限 |
| `DIAG_MAX_CRASH_DUMPS = 5` | 包内转储上限 |

`DiagnosticsResult` 也放在这里：渲染层的 `env.d.ts` 要引用它，而那边不能为一个类型去 import 主进程模块。

### 2.2 `electron/crash-reporter.ts`

- `app.setPath('crashDumps', <userData>/Crashpad)` — 转储目录**固定**在 userData 下。默认位置随平台/版本变（Windows 常在 `%TEMP%` 下），诊断包按固定路径取，不固定就永远是 0 份。
- `crashReporter.start({ uploadToServer: false, compress: false, productName, globalExtra: { 'app.version': … } })` — **只收集不上传**。本项目没有崩溃收集服务，任何"顺手联网"都不可接受；`globalExtra` 里带版本，因为只拿到一份 `.dmp` 时第一件事就是确认它来自哪个版本。
- `app.on('render-process-gone')` / `app.on('child-process-gone')` 各落一条 error 日志：转储说明"崩了"，日志说明"崩的是谁、什么原因（reason/exitCode）"。
- **幂等**且**绝不抛**：启不来只是少一个现场，不能让应用起不来；启动失败只 warn，事件监听照挂。
- 必须在 `app.whenReady()` 之前调用（`main.ts` 模块顶层），否则首屏前后崩的渲染进程抓不到。

### 2.3 `electron/diagnostics.ts`

`exportDiagnostics(opts): Promise<DiagnosticsResult>`，流程：

```
mkdir <userData>/diagnostics/.staging-<pid>-<ts>/{logs,crashes}
  ├─ 白名单日志 → tailText(4MB) → redactSecrets → logs/<原名>
  ├─ <userData>/Crashpad 递归找 *.dmp → 按 mtime 取最近 5 份 → crashes/<NN>-<原名>
  ├─ system.json（buildSystemInfo）
  └─ README.txt（包内说明 + "仍可能含主机名/路径，发出前自行确认"）
tar.exe -czf <userData>/diagnostics/<name>.tar.gz -C <staging> .
  └─ 成功：删 staging，并清掉历史失败残留的 .staging-*
```

要点：

- **日志走白名单**（`main.log`/`.1`/`.2`、`electron-bridge.log`/`.old`），不扫 `*.log`。userData 里还可能有用户自己放的文件。
- **`.dmp` 不脱敏**：二进制改写等于毁证，靠"只在包内、用户自己决定发不发"来控制。
- **失败时保留 staging**：tar 挂了但日志已脱敏写好，用户仍能手工取走；下次成功导出会一并清掉旧 staging。
- `tarCreate` 是注入点（单测替换），其余全部真文件系统 —— 这套逻辑的错误几乎都来自"路径与真实目录不一致"，用假 fs 测不出来。

### 2.4 IPC 与 UI

- `diag:export` → `DiagnosticsResult`；`diag:reveal` → 包还在就 `shell.showItemInFolder`，已删/移则退回产物目录（`shell.openPath`）。均走 `handleTrusted`。
- preload 暴露 `diagExport` / `diagReveal`；类型经 `shared/diagnostics-core` 的 inline import 进 `env.d.ts`。
- 运维面板新增「诊断包」区块（`src/composables/useDiagnosticsBundle.ts` + `OpsPanel.vue`）：导出按钮、结果路径/体积/日志份数/转储份数、失败原因（含保留的 staging 路径）。
- 浏览器模式（无 `window.electronAPI`）→ 按钮置灰并说明原因，而不是点了没反应。

## 3. 脱敏规则（顺序有意义）

| 顺序 | 规则 | 例子 |
|---|---|---|
| 1 | Bearer token | `Authorization: Bearer eyJ…` → `Bearer ***` |
| 2 | `sk-` 厂商前缀 | `sk-proj-abcdefghij` → `sk-***` |
| 3 | `gh[pousr]_` 令牌 | `ghp_abcdefghijklmnop` → `ghp_***` |
| 4 | URL userinfo | `http://user:pw@proxy:3128` → `http://***@proxy:3128`（主机端口保留，排障要看指向哪） |
| 5 | `key=value` / `key: value` / `"key": "value"` | `ELECTRON_BRIDGE_TOKEN=abcdef` → `…=***` |

两条踩过的坑：

- **Bearer 必须先于键值规则**。否则 `Authorization: Bearer eyJ…` 会先被键值规则吃掉 `Bearer` 这一段，令牌本体停在 `***` 后面（`diagnostics-core.test.ts` 里有对应用例）。
- **键名允许带前缀词**（`ELECTRON_BRIDGE_TOKEN`、`CKUN_API_KEY`）：只认裸 `token` 会全部漏掉（下划线是单词字符，`\btoken` 在 `_TOKEN` 里不成立）。
- 值不足 4 个字符不抹：`password: ` 这类空值抹了只会让日志更难看。

## 4. 体积与取舍

- 主日志三份最多 15MB，全塞进包没有意义 → 每份留尾部 4MB（排障看最近发生了什么，头部是启动样板）。
- 转储只取最近 5 份：`.dmp` 单份可达几十 MB，历史转储对"这次为什么崩"没有帮助。
- 最终包通常 < 10MB，可以直接走聊天工具发出。

## 5. 失败姿态

| 场景 | 行为 |
|---|---|
| `userData` 不可写 | 转储目录退回 Electron 默认位置，只 warning；诊断包导出会失败并说明 |
| `crashReporter.start` 抛错 | warning，事件监听照挂，应用照常启动 |
| 单个日志读失败（被占用） | 跳过该文件，其余照打 |
| 转储目录不存在 | 视为从未崩溃，`crashDumps: 0`，不是错误 |
| `tar.exe` 失败 | `ok:false` + 不产包 + staging 保留（错误信息里带路径） |
| `diag:reveal` 时包已被删 | 退回打开产物目录；目录也没有 → 抛「还没有导出过诊断包」 |

## 6. 测试

- `shared/diagnostics-core.test.ts`（13）：按"真实日志长什么样"写脱敏用例（env 风格键值、Bearer、`sk-`/`ghp_`、代理口令保留主机、短值不动、幂等、中文不动）、`tailText` 多字节安全、文件名无冒号、env 白名单、`system.json` 摊平且不含凭据。
- `electron/diagnostics.test.ts`（7）：白名单（`notes.log` 不进包）、脱敏落到包内内容、尾截断、转储取最近 5 份并带序号、空包、tar 失败保留 staging、下次成功清掉旧 staging、通道口令不出包。
- `electron/crash-reporter.test.ts`（4）：转储目录固定 + `uploadToServer:false` + `globalExtra` 版本、崩溃事件落日志、start 抛错只 warn（事件仍挂）、幂等。
- `electron/diagnostics-e2e.test.ts`（Windows，1）：**真 tar.exe** 打包再解开核对（`system.json`/`logs/main.log`/`crashes/*.dmp` 都在、日志已脱敏、转储字节一致、无 staging 残留）。单测里 tar 是注入的，参数顺序与相对路径错了它永远发现不了。
- `src/composables/useDiagnosticsBundle.test.ts`（6）：成功落结果、`ok:false` 展示主进程的错误、IPC 抛错出声、导出中忽略重复点击、浏览器模式不发 IPC、reveal 被拒出声。

## 7. 明确不做

- **上传/聚合**：没有崩溃收集服务，也没有凭据管理的运营承诺；诊断包只落本地，由用户决定发给谁。
- **minidump 符号化**：需要为每次构建归档 PDB 并维护符号服务器，成本远大于收益。
- **自动在崩溃后弹「是否发送诊断包」**：崩溃现场最需要的是让用户继续干活；面板上的手动导出足够。
- **诊断包里带数据库/会话内容**：只带结构性信息（表名行数在后端 `/api/ops/diagnostics` 里，不在这里）。
