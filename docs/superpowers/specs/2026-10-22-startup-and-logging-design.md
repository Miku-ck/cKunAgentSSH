# 开发启动速度与日志管道优化设计

- 日期：2026-10-22
- 范围：`npm run dev` 启动链路（Electron 主进程 + Python 后端）与主进程/后端/渲染层日志
- 状态：设计已确认，待实施

## 1. 背景与实测

热启动（第二次 `npm run dev`，文件缓存已热）：

| 里程碑 | 相对启动耗时 |
| --- | --- |
| electron-vite 开始构建 main | 435ms |
| main/preload 构建完成 + dev server 就绪 | 852ms |
| Electron 拉起 | 862ms |
| `Using Python interpreter` | 2695ms |
| uvicorn 监听 + 应用启动完成 | 3491ms |
| 桥接 WS 连上（后端可服务） | 3666ms |

冷启动（首次）同一链路中，孤儿枚举曾占 ~9.5s；node_modules 首次扫描另占 ~9.6s（环境/杀软成因，不在本次代码改动范围）。

拆解后，spawn 之前的两个串行步骤与本进程无关、却决定后端就绪时间：

1. 孤儿进程枚举（PowerShell CIM）：热 1.3s，冷最高 ~9.5s。
2. Python 解释器探测（`import fastapi, uvicorn`）：0.6-1s。

日志现状：

- 后端 stdout 被直接丢弃（`backend-manager.ts` 的空监听器），uvicorn access 日志完全不可见。
- 后端 stderr 一律以 `console.error('[Python] ...')` 输出：INFO 启动行也显示为错误，且与主进程 `[main]`/`[BackendManager]` 混在一起，无统一时间戳。
- 主进程、后端日志都不落盘，关窗即丢。

## 2. 目标与非目标

目标：

- 热启动后端就绪从 ~3.6s 降到 ≤1.5s；冷启动不再被 WMI 枚举垫高。
- 控制台按来源与级别统一格式，默认不刷 uvicorn access 行。
- 主进程 + 后端 + 渲染层（warn/error）日志落盘，按大小轮转。
- 启动耗时可在日志中验收。

非目标：

- 不优化 electron-vite / Vite 首次冷扫描（环境与杀软成因）。
- 不改打包产物启动路径、不改后端业务逻辑。
- 不引入第三方日志依赖。
- 不做日志查看 UI。

## 3. 启动关键路径改造

### 3.1 孤儿枚举移出 spawn 前关键路径

现状：`start()` 里 `await orphansPromise` 成功后才 spawn，理由是"spawn 后再枚举会把刚拉起的后端当孤儿杀掉"。

改法：

- 枚举仍在 `start()` 入口启动（此时新进程尚不存在），但不再 await。
- spawn 成功后执行 `void orphansPromise.then(pids => killPids(pids, [child.pid, process.pid]))`。
- 防误杀依据由"时序"改为"身份"：排除刚 spawn 的 `child.pid` 与主进程 PID；`reload=False` 后端不派生子进程，PID 排除足够。
- `killOrphanBackends` / `killPids` 注释中的"必须在 spawn 前完成枚举"契约同步改写为身份排除契约。

失败语义不变：枚举失败仍 `resolve([])`，只影响孤儿清理，不影响启动。

### 3.2 Python 探测降级为存活检查

现状：`probePython` 执行 `-c "import fastapi, uvicorn"`（~600ms），用于把"解释器在但没装依赖"挡在 spawn 前。

改法：

- 探测命令改 `-c ""`（~70ms），保留三候选并行与 Microsoft Store 存根（exit 9009）识别。
- 依赖缺失改由启动失败链路兜底：`childExitPromise`/健康检查超时会附带 stderr 尾部；`backend-manager` 在 stderr 尾部匹配 `ModuleNotFoundError: No module named '(fastapi|uvicorn)'` 时，追加可操作提示（安装 `backend/requirements.txt` 或 `CKUN_PYTHON` 指向已装依赖的解释器）。
- `resolvePythonCommand` 的"全部候选不可用"错误文案保留 `未找到可运行后端的 Python 环境` 与候选明细，去掉"需要 fastapi/uvicorn"的因果暗示。

权衡：多解释器机器上若 `python` 可执行但缺依赖、`python3` 有依赖，原实现会选中 `python3`，新实现会选中 `python` 并在启动阶段失败。该场景罕见，失败信息明确且可操作；换取每次启动省 0.5-1s。

### 3.3 启动里程碑

- 主进程模块加载记 `T0`。
- `ready-to-show`：`[startup] window-visible=+{n}ms`
- `did-finish-load`：`[startup] renderer-loaded=+{n}ms`
- 后端就绪：`[startup] backend-ready in {n}ms (port=..., interpreter=...)`
- 后端失败：`[startup] backend-failed in {n}ms: {message}`

均为 info 级，一次启动固定 3-4 行。

## 4. 日志管道

### 4.1 模块与 API（`electron/logger.ts`，零依赖）

```ts
type LogLevel = 'debug' | 'info' | 'warn' | 'error'

export function initLogging(opts?: { consoleLevel?: LogLevel; fileLevel?: LogLevel }): void
export function createLogger(scope: string): {
  debug(...args: unknown[]): void
  info(...args: unknown[]): void
  warn(...args: unknown[]): void
  error(...args: unknown[]): void
}
export function attachBackendLogs(
  child: { stdout: NodeJS.ReadableStream | null; stderr: NodeJS.ReadableStream | null },
  onStderr?: (text: string) => void,
): void
export function getLogDir(): string
export function flushLogs(): Promise<void>
```

- 控制台格式：`HH:mm:ss.SSS LEVEL [scope] message`；TTY 下级别着色，非 TTY 纯文本。
- 文件格式：`YYYY-MM-DD HH:mm:ss.SSS LEVEL [scope] message`。
- 级别来源：`CKUN_LOG_LEVEL` 环境变量；默认 `info`。非法值回落 `info`。
- `attachBackendLogs` 内部按行缓冲（行可能跨 chunk），并按 4.2 规则解析级别与 access 行。

### 4.2 后端输出规则

- stdout：默认 info；命中 `- "GET|POST|..."` 视为 access 行。全部写 `backend.log`。
- stderr：按 `^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+`（uvicorn）或 `^\d{2}:\d{2}:\d{2}\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+`（本项目 logconfig 格式）解析级别；连续行（traceback）继承上一行级别。全部写 `backend.log`。
- 控制台：级别 ≥ 当前 consoleLevel 的后端行才输出（默认 info，所以启动 INFO 可见）；access 行只在 consoleLevel=debug 时输出。
- `stderrTail` 4KB 环形缓存保留，仍用于启动失败错误消息；由 `onStderr` 回调喂入。
- 文件行格式：`... INFO  [backend] <原始行>`；原始行自带的时间戳若命中 `^\d{2}:\d{2}:\d{2}\s+` 则剥掉，避免双时间戳。

### 4.3 主进程日志

- `initLogging()` 在 `main.ts` 模块顶部（imports 之后）调用。
- 包装 `console.log/info/warn/error/debug`：控制台外观不变，同时写入 `app.log`（带时间戳与级别）。logger 内部改用包装前的原始 console，避免双写。
- 参数序列化用 `node:util.format`；不可序列化对象降级为 `String()`。
- 迁移范围：`main.ts` 与 `backend-manager.ts` 的启动相关日志改用 `createLogger('main' | 'backend')`；其余模块的既有 `console.*` 由包装层落盘，不强制迁移。`SSH_DEBUG` 的 ssh2 传输日志保持原样（仍受开关控制）。

### 4.4 渲染层捕获

- `webContents.on('console-message')`：warn/error 级别写 `app.log`（scope `renderer`），不重复上控制台（DevTools 已有）。
- `did-fail-load` / `render-process-gone`：error 级写入 `app.log`。

### 4.5 文件与轮转

- 目录：`app.getPath('userData')/logs`，启动时打印一次 `[log] 目录: <path>`。
- 文件：`app.log`（主进程 + 渲染层）、`backend.log`（后端两路管道）。
- 轮转：每文件 5MB × 3 份；超过上限时 `app.log → app.1.log → app.2.log`，最旧删除。
- 写入：`fs.createWriteStream({ flags: 'a' })`，打开时 `statSync` 记初始大小；每次写入累加并在超限时轮转。

### 4.6 降级与错误处理

- 日志目录创建/打开失败：本次进程内禁用文件 sink，原始 console 打一条一次性警告，之后只输出控制台，绝不抛错、绝不递归记录。
- 轮转失败（文件被占用等）：继续追加当前文件，不中断。
- 后端管道 `EPIPE`/已销毁：静默忽略（沿用现状）。
- `will-quit` 时 `flushLogs()`（`end()` 两个流），不阻塞退出。

## 5. 文件影响面

新增：

- `electron/logger.ts`
- `electron/logger.test.ts`

修改：

- `electron/backend-manager.ts`
- `electron/main.ts`
- `electron/backend-manager-python-resolve.test.ts`（探测命令断言改为 `['-c', '']`；补"依赖缺失由启动失败兜底"的文案断言）

不改：后端 Python 代码、Vite 配置、`electron.vite.config.ts`。

## 6. 测试计划

单元（vitest，`npm test`）：

- logger 格式：控制台/文件行格式、级别过滤、`CKUN_LOG_LEVEL` 非法值回落。
- 行缓冲：一次 chunk 多行、半行跨 chunk。
- 后端级别解析：uvicorn INFO、logconfig 格式、access 行识别、traceback 续行继承级别。
- 轮转：小上限下连续写入触发 `app.log → app.1.log`，最旧被删；目录不可写时降级不抛错。
- backend-manager：孤儿枚举不阻塞 spawn（枚举 Promise 悬挂时 spawn 仍发生）；探测命令为存活检查；`ModuleNotFoundError` 尾部产出可操作提示。

静态与回归：

- `npm run typecheck`（`vue-tsc` web + node）
- 既有 `electron/backend-manager-*.test.ts` 全绿

手工验收：

- `npm run dev` 连续两次，日志中 `[startup] backend-ready` 热启动 ≤1.5s。
- 控制台无 `127.0.0.1 - "GET /api/..."` access 行；`INFO: Started server process` 以 info 呈现。
- `%APPDATA%/cKunAgentSSH/logs/app.log` 与 `backend.log` 存在，含启动里程碑与后端 access 行。
- 人为把 `CKUN_PYTHON` 指向无依赖解释器：启动失败信息包含 `ModuleNotFoundError` 与安装提示。

## 7. 风险

- 孤儿枚举与 spawn 并行：依赖"新进程 PID 排除 + 后端无子进程"成立；若将来开启 uvicorn reload，需要改成按 CreationDate 截止过滤。
- 探测不校验依赖：多解释器且首选缺依赖时启动失败一次；错误信息已给出明确修复路径。
- console 包装：若第三方库直接写 stdout 而非 console，不会进 app.log；后端管道与渲染层捕获已覆盖主要来源。
