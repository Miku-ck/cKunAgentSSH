# 启动提速 + tar-only 分发收敛 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端就绪链去串行化（backend ready ≤1.6s），发布链路收敛为 tar.gz，tar 形态下桌面/开始菜单快捷方式与 AUMID 通知归属自建补齐。

**Architecture:** `backend-manager.ts` 内改造孤儿枚举汇合时机 + 新增解释器缓存模块；新增 `shortcut-win.ts` 用 PowerShell（WScript.Shell 建 .lnk + IPropertyStore 写 AUMID）补安装器职责；`electron-builder.yml`/`package.js`/`ci.yml` 三处收敛为 `dir` 产物 + tar 出件。

**Tech Stack:** TypeScript(Electron main)、Node 内置模块、vitest(node 环境)、PowerShell、tar.exe、electron-builder。

**Spec:** `docs/superpowers/specs/2026-10-23-startup-speed-tar-packaging-design.md`（含审计基线与验收口径，实施前先读）

## Global Constraints

- 代码注释一律中文（项目规范）；
- 不新增任何运行时依赖（package.json dependencies 不动）；
- `appId: com.ckun.agent-ssh` 与 `APP_USER_MODEL_ID` 不改值，守卫测试 `electron/app-user-model-id.test.ts` 必须保持绿；
- 不引入新 electron-builder 下载物（tar 用系统自带 tar.exe / POSIX tar）；
- 工作区存在他人 WIP：每步提交只 `git add` 本任务列出的文件，严禁 `git add -A`；
- 测试命令统一 `npx vitest run <file>`（仓库根执行）。

---

### Task 1: A1 孤儿枚举汇合移出 spawn 前阻塞

**Files:**
- Modify: `electron/backend-manager.ts`（`start()` L303-377 区段、`killOrphanBackends` docstring L183-201）
- Test: `electron/backend-manager-orphan-timing.test.ts`（新建）

**Interfaces:**
- Produces: `start()` 行为契约——"枚举必须始于 spawn 前、汇合允许晚于 spawn，防误杀靠 killPids exclude"；`[perf] backend: spawned` 打点时刻不再晚于 orphan 枚举完成时刻。

- [ ] **Step 1: 写失败测试（时序契约）**

新建 `electron/backend-manager-orphan-timing.test.ts`，要点（完整骨架）：

```ts
/**
 * A1 时序契约回归：孤儿枚举的 await 汇合必须允许晚于 spawn（不再垫高启动
 * 关键路径），而防误杀由 killPids 的 exclude 承担 —— 新后端的 pid 出现在
 * 枚举结果里也不能被 taskkill。
 */
import { EventEmitter } from 'events'
import { describe, expect, it, vi, beforeEach, afterEach, type Mock } from 'vitest'

vi.mock('electron', () => ({
  app: { getPath: () => '', getAppPath: () => '/fake/app', isPackaged: false },
}))
vi.mock('fs', async (orig) => {
  const actual = await orig<typeof import('fs')>()
  return { ...actual, existsSync: () => true }
})
vi.mock('child_process', async (orig) => {
  const actual = await orig<typeof import('child_process')>()
  return { ...actual, execFile: vi.fn(), spawn: vi.fn() }
})

import { execFile, spawn } from 'child_process'
import { BackendManager } from './backend-manager'

const execFileMock = execFile as unknown as Mock
const spawnMock = spawn as unknown as Mock

// 每个用例重建实例并还原原型桩，避免实例间状态串味
const proto = BackendManager.prototype as unknown as Record<string, unknown>
const saved: Record<string, unknown> = {}

beforeEach(() => {
  for (const k of ['findFreePort', 'resolvePythonCommand', 'ensureDbPath', 'getBackendDir', 'waitForReady']) {
    saved[k] = proto[k]
  }
  proto.findFreePort = () => Promise.resolve(8010)
  proto.resolvePythonCommand = () => Promise.resolve({ cmd: 'python', prefixArgs: [] })
  proto.ensureDbPath = () => '/tmp/ckun-fake.db'
  proto.getBackendDir = () => '/fake/app/backend'
  proto.waitForReady = () => new Promise<void>((r) => { setTimeout(r, 0) })
})
afterEach(() => {
  Object.assign(proto, saved)
  execFileMock.mockReset()
  spawnMock.mockReset()
})

/** 可控的 CIM 枚举：回调扣在手里，由测试决定何时放行 */
function stubEnumeration() {
  let cimCb: ((e: Error | null, out: string) => void) | null = null
  execFileMock.mockImplementation((cmd: string, args: unknown[], _o: unknown, cb: (e: Error | null, out: string) => void) => {
    if (typeof args[0] === 'string' && String(args[0]).includes('Get-CimInstance')) {
      cimCb = cb
    } else {
      process.nextTick(() => cb(null, '')) // taskkill 等：立即成功
    }
  })
  return {
    /** 放行枚举，rows 里带上 newPid（模拟"枚举快照晚于 spawn 仍会捞到新进程"）；
     * 命令行必须带 --host 127.0.0.1 + \backend\main.py，否则会被 isOurBackendProcess 的
     * 宽口径谓词过滤掉，taskkill 断言就变成对空列表的恒真断言 */
    resolve(newPid: number) { cimCb!(null, `9999|D:\\app\\backend\\main.py --port 8011 --host 127.0.0.1\r\n${newPid}|D:\\app\\backend\\main.py --port 8010 --host 127.0.0.1`) },
  }
}

function stubSpawn() {
  const child = new EventEmitter() as EventEmitter & { pid: number; stdout: null; stderr: null }
  child.stdout = null
  child.stderr = null
  spawnMock.mockReturnValue(child)
  return child
}

describe('A1 孤儿枚举汇合时机', () => {
  it('spawn 不被枚举 await 阻塞：枚举未回，spawn 已发生', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const enumStub = stubEnumeration()
    const child = stubSpawn()
    child.pid = 4321
    const bm = new BackendManager()
    const p = bm.start()
    await new Promise((r) => setImmediate(r))
    expect(spawnMock).toHaveBeenCalledTimes(1) // 枚举回调还没放行
    enumStub.resolve(child.pid)
    await p
    // taskkill 只许对旧孤儿 9999 下手；新后端 pid 出现在枚举结果里也必须被 exclude 拦下
    const killed = execFileMock.mock.calls
      .filter((c) => c[0] === 'taskkill' || String(c[0]).endsWith('taskkill.exe'))
      .map((c) => (c[1] as string[]).join(' '))
    expect(killed.some((a) => a.includes('/PID 9999'))).toBe(true)
    expect(killed.some((a) => a.includes('/PID 4321'))).toBe(false)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run electron/backend-manager-orphan-timing.test.ts`
Expected: FAIL —— 现实现 `start()` 在 spawn 前 `await orphansPromise`，`spawnMock` 断言处收到 0 次调用。

- [ ] **Step 3: 改造 `start()` 汇合时机**

`electron/backend-manager.ts`：删除 L323-324 的 `const orphans = await orphansPromise`（保留 `const portProbe = ...` 与 `this.port = await portProbe`），把 spawn 后的 L376-378 改为：

```ts
      const child = this.process
      perfMark(`backend: spawned pid=${child.pid}`)
      // A1：枚举快照必须始于 spawn 前（上面 killOrphanBackends 的调用位置），
      // 但汇合允许晚于 spawn —— 误杀防护由 killPids 的 exclude 承担（新 pid +
      // 自身 pid），不再靠 spawn 前 await 垫高启动关键路径（实测省 1.1~1.3s）。
      void (async () => {
        try {
          const orphans = await orphansPromise
          perfMark('backend: orphan enumeration done')
          BackendManager.killPids(orphans, [child.pid ?? -1, process.pid])
        } catch (e) {
          console.warn('[BackendManager] 孤儿枚举/杀灭失败（不影响本次启动）:', e)
        }
      })()
```

同步改写 `killOrphanBackends` docstring（L183-201）的"时序契约（防误杀）"段为上述新口径，删除 `start()` 内 L307-310 注释里"必须在 spawn 前 await"的旧表述；`appQuitting` 守卫（L330-333）位置不动，但其注释中"spawn 之前的最后一个 await 恢复点"需按新结构改为"spawn 前的 appQuitting 检查（枚举汇合已后移，不再构成 await 恢复点）"。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npx vitest run electron/backend-manager-orphan-timing.test.ts electron/backend-manager-python-resolve.test.ts electron/backend-manager-db-migration.test.ts electron/backend-manager-missing-entry.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add electron/backend-manager.ts electron/backend-manager-orphan-timing.test.ts electron/utils/perf-mark.ts
git commit -m "perf(backend): 孤儿枚举汇合移出 spawn 前阻塞，误杀防护改由 exclude 单点承担"
```

说明：`electron/utils/perf-mark.ts` 及 main.ts 里的 `[perf]` 打点（spec §5 定案为常驻）随本任务首次入库；main.ts 的打点改动在 Task 4 提交（它同时含接线改动）。

---

### Task 2: A2 Python 解释器乐观缓存

**Files:**
- Create: `electron/backend-python-cache.ts`
- Create: `electron/backend-python-cache.test.ts`
- Modify: `electron/backend-manager.ts`（`resolvePythonCommand`、`start()` 成功/失败路径）

**Interfaces:**
- Consumes: Task 1 后的 `start()` 结构。
- Produces: `backend-python-cache.ts` 导出：

```ts
export interface PythonLaunch { cmd: string; prefixArgs: string[] }
export const PYTHON_CACHE_FILE = 'backend-python.json'
export function loadPythonCache(userDataDir: string): PythonLaunch | null   // 损坏/缺失 → null，不抛
export function savePythonCache(userDataDir: string, launch: PythonLaunch): void // 异步原子写，失败仅 warn
export function clearPythonCache(userDataDir: string): void                 // 幂等删除，失败仅 warn
```

- [ ] **Step 1: 写失败测试（缓存模块）**

新建 `electron/backend-python-cache.test.ts`：

```ts
/**
 * 解释器缓存模块：读-写-清三态 + 损坏文件按无缓存处理（与 desktop-prefs 同口径：
 * 缓存永远不该把启动打崩，也不该把脏值喂给 spawn）。
 */
import { mkdtempSync, rmSync, writeFileSync, readFileSync, existsSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { clearPythonCache, loadPythonCache, PYTHON_CACHE_FILE, savePythonCache } from './backend-python-cache'

let dir = ''
beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'ckun-py-cache-')) })
afterEach(() => { rmSync(dir, { recursive: true, force: true }) })

describe('backend-python-cache', () => {
  it('缺失 → null；round-trip 保真；flush 等待异步写完成', () => {
    expect(loadPythonCache(dir)).toBeNull()
    savePythonCache(dir, { cmd: 'python', prefixArgs: ['-3'] })
    savePythonCache(dir, () => {}) // 非法值不落盘（内部校验）
    // savePythonCache 为异步写：轮询等待最多 1s（见 Step 3 的 flush 语义说明）
    for (let i = 0; i < 20 && !existsSync(join(dir, PYTHON_CACHE_FILE)); i++) {
      const t = Date.now(); while (Date.now() - t < 50) { /* spin */ }
    }
    expect(loadPythonCache(dir)).toEqual({ cmd: 'python', prefixArgs: ['-3'] })
    clearPythonCache(dir)
    expect(existsSync(join(dir, PYTHON_CACHE_FILE))).toBe(false)
    expect(() => clearPythonCache(dir)).not.toThrow() // 幂等
  })
  it('损坏 JSON / 脏结构 → null 且可被覆盖', () => {
    writeFileSync(join(dir, PYTHON_CACHE_FILE), '{oops')
    expect(loadPythonCache(dir)).toBeNull()
    writeFileSync(join(dir, PYTHON_CACHE_FILE), JSON.stringify({ cmd: 42, prefixArgs: 'x' }))
    expect(loadPythonCache(dir)).toBeNull()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run electron/backend-python-cache.test.ts`
Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 `backend-python-cache.ts`**

要点：`loadPythonCache` 用同步 `readFileSync` + `JSON.parse` try/catch，校验 `typeof cmd === 'string' && cmd && Array.isArray(prefixArgs) && prefixArgs.every(s => typeof s === 'string')`，不满足返回 null；`savePythonCache` 先做同套校验（非法值直接 warn 返回），再走 `writeFile(tmp) → rename`（EPERM/EACCES/EBUSY 等 200ms 重试一次、仍失败退回直写——与 desktop-prefs `writeToDisk` 同一模式），整体 fire-and-forget 不返回 Promise（内部 `.catch(warn)`）；`clearPythonCache` 用 `rmSync(force)` 包 try。文件头中文 docstring 说明"乐观缓存：命中直接 spawn，健康就绪前失败一律作废"。

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run electron/backend-python-cache.test.ts`
Expected: PASS。

- [ ] **Step 5: 写失败测试（resolvePythonCommand 缓存优先 + start 联动）**

新建 `electron/backend-manager-python-cache.test.ts`，沿用 Task 1 测试的 mock 骨架（electron/fs/child_process 三件套）：

```ts
describe('A2 解释器乐观缓存', () => {
  it('缓存命中：resolvePythonCommand 零探测直接返回，且 cmd 需在盘上存在', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const ud = mkdtempSync(join(tmpdir(), 'ckun-py-hit-'))
    savePythonCache(ud, { cmd: 'python', prefixArgs: [] })
    // 等异步写落盘（同 Task Step1 的轮询等待）
    // app.getPath('userData') → ud；existsSync('python') stub true
    const r = await resolve()
    expect(r).toEqual({ cmd: 'python', prefixArgs: [] })
    expect(execFileMock).not.toHaveBeenCalled() // 关键路径零探测
  })
  it('缓存的 cmd 不存在 → 回退全量并行探测', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const ud = mkdtempSync(join(tmpdir(), 'ckun-py-miss-'))
    savePythonCache(ud, { cmd: 'D:/gone/python.exe', prefixArgs: [] })
    // existsSync 桩：对缓存 cmd 返回 false，其余（main.py 入口检查）返回 true
    existsMock.mockImplementation((p: string) => p !== 'D:/gone/python.exe')
    stubProbe(new Set(['python'])) // 全量探测 python 命中
    const r = await resolve()
    expect(r).toEqual({ cmd: 'python', prefixArgs: [] })
    expect(execFileMock).toHaveBeenCalledTimes(3) // 确实走了一轮并行探测
  })
  it('健康就绪前失败 → 缓存作废，下一次 start 走全量探测', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const ud = mkdtempSync(join(tmpdir(), 'ckun-py-inval-'))
    savePythonCache(ud, { cmd: 'python', prefixArgs: [] })
    // waitForReady stub 改为 reject（模拟健康超时）
    proto.waitForReady = () => Promise.reject(new Error('health timeout'))
    const bm = new BackendManager()
    await expect(bm.start()).rejects.toThrow(/health timeout/)
    expect(loadPythonCache(ud)).toBeNull() // 缓存已作废
  })
  it('start 成功（health ready）→ 本次解释器写入缓存', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true })
    const ud = mkdtempSync(join(tmpdir(), 'ckun-py-save-'))
    // 无缓存 → 全量探测命中 python；就绪后应落盘 { cmd: 'python', prefixArgs: [] }
    stubProbe(new Set(['python']))
    const bm = new BackendManager()
    await bm.start()
    for (let i = 0; i < 20 && loadPythonCache(ud) === null; i++) {
      await new Promise((r) => setTimeout(r, 50)) // savePythonCache 异步写，轮询等待
    }
    expect(loadPythonCache(ud)).toEqual({ cmd: 'python', prefixArgs: [] })
  })
})
```

骨架补充：本文件除 Task 1 的三件套 mock 外，还需 ① `vi.mock('fs')` 暴露可控的 `existsMock`（默认全 true，供"缓存 cmd 不存在"用例改写）；② `app.getPath` 按 `'userData'` 返回当前用例的临时目录；③ 涉及 `start()` 的两个用例复用 Task 1 的 `stubSpawn()`/`stubProbe` 风格（execFile 桩需同时应答 CIM 枚举与探测调用：命令行含 `Get-CimInstance` 则回空结果，含 `import fastapi` 则按 passSet 判定）。

- [ ] **Step 6: 接入 `backend-manager.ts`**

- `resolvePythonCommand()`：在 CKUN_PYTHON 与 bundled-runtime 两级之后、并行探测之前插入缓存层——`const cached = loadPythonCache(app.getPath('userData')); if (cached && existsSync(cached.cmd)) { perfMark('backend: python cache hit'); return cached }`；全量探测成功不在此处写缓存（乐观策略：就绪后才可信）。
- `start()`：记录本次使用的 `launch`；`markReady()` 成功分支后 `savePythonCache(app.getPath('userData'), launch)`；spawn 后失败路径（`Promise.race` 的 catch）与 spawn 前 catch 均调 `clearPythonCache(app.getPath('userData'))`。
- docstring 说明失效判定口径：健康就绪前任何失败（ENOENT/9009/超时）即缓存失真。

- [ ] **Step 7: 跑测试确认通过 + 回归**

Run: `npx vitest run electron/backend-python-cache.test.ts electron/backend-manager-python-cache.test.ts electron/backend-manager-python-resolve.test.ts electron/backend-manager-orphan-timing.test.ts`
Expected: 全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add electron/backend-python-cache.ts electron/backend-python-cache.test.ts electron/backend-manager-python-cache.test.ts electron/backend-manager.ts
git commit -m "perf(backend): 解释器乐观缓存，命中路径关键探测零开销"
```

---

### Task 3: PowerShell 路径解析提取为共享 util

**Files:**
- Create: `electron/utils/powershell-path.ts`
- Create: `electron/utils/powershell-path.test.ts`
- Modify: `electron/backend-manager.ts`（`resolvePowerShell` 改为委托）

**Interfaces:**
- Produces: `export function resolvePowerShell(exists?: (p: string) => boolean): string`（默认实现用 `existsSync`，签名与 backend-manager 现私有方法一致，供 Task 4 复用）。

- [ ] **Step 1: 写失败测试**

`electron/utils/powershell-path.test.ts`：注入 `exists` 桩，断言 System32 命中优先、Sysnative 兜底、全缺返回裸名 `'powershell'`（三条用例，逐条断言返回值）。

- [ ] **Step 2: 跑测试确认失败** → Run: `npx vitest run electron/utils/powershell-path.test.ts`，Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 util + backend-manager 委托**

util 内容 = 现 `BackendManager.resolvePowerShell` 逻辑平移（候选表 + 注释保留，参数化 `exists` 便于测）。`backend-manager.ts` 私有静态方法改为一行 `return resolvePowerShell()`，删重复实现体。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npx vitest run electron/utils/powershell-path.test.ts electron/backend-manager-python-resolve.test.ts`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add electron/utils/powershell-path.ts electron/utils/powershell-path.test.ts electron/backend-manager.ts
git commit -m "refactor(electron): PowerShell 绝对路径解析提取为共享 util"
```

---

### Task 4: .lnk/AUMID 快捷方式生成器

**Files:**
- Create: `electron/shortcut-win.ts`
- Create: `electron/shortcut-win.test.ts`
- Modify: `electron/main.ts`（whenReady 内接线）

**Interfaces:**
- Consumes: Task 3 `resolvePowerShell()`；`desktop-prefs.ts` 的 `APP_USER_MODEL_ID`（只 import 常量，不复制字面量）。
- Produces:

```ts
export interface ShortcutPlan { createStartMenu: boolean; createDesktop: boolean }
export function shellQuotePs(s: string): string                // PS 单引号转义 ''，所有插入 PS 的字符串必须过它
export function startMenuLnkPath(appData: string): string      // <appData>\Microsoft\Windows\Start Menu\Programs\cKunAgentSSH.lnk
export function planShortcuts(opts: { startMenuExists: boolean; desktopMarkerExists: boolean }): ShortcutPlan
export function buildShortcutScript(plan: ShortcutPlan, execPath: string, aumid: string): string
// 桌面/开始菜单实际路径在 PS 内用 [Environment]::GetFolderPath 解析（OneDrive 重定向兼容），
// TS 侧只对开始菜单做 existsSync 幂等判断（app.getPath('appData') 可确定性拼出），
// 桌面项靠 userData 标记文件 .desktop-shortcut-created 控制仅首启一次。
export async function ensureShellShortcuts(): Promise<void>     // 非 win/非打包态直接 return；幂等
```

- [ ] **Step 1: 写失败测试**

`electron/shortcut-win.test.ts`（mock electron：`isPackaged`/`getPath` 可控；mock child_process execFile；mock fs existsSync/writeFileSync 可控）：

```ts
describe('shortcut-win', () => {
  it('buildShortcutScript：全量计划含 WScript.Shell、PKEY GUID、AUMID 字面量与 Desktop 解析', () => {
    const s = buildShortcutScript({ createStartMenu: true, createDesktop: true }, 'D:\\app\\cKunAgentSSH.exe', 'com.ckun.agent-ssh')
    expect(s).toContain('WScript.Shell')
    expect(s).toContain('9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3') // PKEY_AppUserModel_ID fmtid
    expect(s).toContain('com.ckun.agent-ssh')
    expect(s).toContain("GetFolderPath('Desktop')")
    expect(s).toContain("GetFolderPath('StartMenu')")
  })
  it('非 win32 跳过：execFile 不触发', async () => {
    setPlatform('linux')
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('非打包态跳过（dev 的 electron.exe 不配建快捷方式）', async () => {
    setPlatform('win32')
    packagedMock.value = false
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('开始菜单项已存在且桌面标记已写 → 零 PS 调用（幂等）', async () => {
    setPlatform('win32')
    packagedMock.value = true
    existsMock.mockImplementation((p: string) => p.endsWith('.lnk') || p.endsWith('.desktop-shortcut-created'))
    await ensureShellShortcuts()
    expect(execFileMock).not.toHaveBeenCalled()
  })
  it('PS 失败仅 warn 不抛（快捷方式建不出来不影响应用本体）', async () => {
    setPlatform('win32')
    packagedMock.value = true
    existsMock.mockReturnValue(false)
    execFileMock.mockImplementation((_c: string, _a: string[], _o: unknown, cb: (e: Error) => void) => {
      process.nextTick(() => cb(new Error('ConstrainedLanguage 下 Add-Type 失败')))
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await expect(ensureShellShortcuts()).resolves.toBeUndefined()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('快捷方式创建失败'), expect.anything())
  })
})
```

- [ ] **Step 2: 跑测试确认失败** → Run: `npx vitest run electron/shortcut-win.test.ts`，Expected: FAIL。

- [ ] **Step 3: 实现 `shortcut-win.ts`**

`buildShortcutScript(plan, execPath, aumid)` 生成的 PS 脚本结构（每步 `$ErrorActionPreference='Stop'`，任一步失败整体非零退出、由 TS 侧捕获；按 plan 开关拼接段落，字符串插入一律经 `shellQuotePs` 单引号转义）：

```powershell
$ErrorActionPreference = 'Stop'
# —— 每个启用的目标一段，<folderExpr> 为 GetFolderPath('StartMenu')+'\Programs' 或 GetFolderPath('Desktop')
$dir = <folderExpr>
$lnk = Join-Path $dir 'cKunAgentSSH.lnk'
# 1) WScript.Shell 建 .lnk（TargetPath=execPath，WorkingDirectory=exe 所在目录）
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = '<execPath>'
$sc.WorkingDirectory = Split-Path '<execPath>'
$sc.Description = 'cKunAgentSSH'
$sc.Save()
# 2) Add-Type C#：SHGetPropertyStoreFromParsingName + IPropertyStore.SetValue 写 AUMID
Add-Type -TypeDefinition '<C# 片段>'
[CKunLnkAumid]::Set($lnk, '<aumid>')
```

C# 片段（`-TypeDefinition` 内嵌，x64 PropVariant 布局 `VT_LPWSTR=31`、字段偏移 8）：

```csharp
using System;
using System.Runtime.InteropServices;
public static class CKunLnkAumid {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHGetPropertyStoreFromParsingName(
    [MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr pbc, int flags,
    ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
  [ComImport, Guid("886d8eea-8cf2-4446-8d02-cdba1dbdcf91"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    void GetCount(out uint c); void GetAt(uint i, out PropertyKey k);
    void GetValue(ref PropertyKey k, out PropVariant v); void GetCaps(out uint c);
    void SetValue(ref PropertyKey k, ref PropVariant v); void Commit();
  }
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)]
  struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
  }
  public static void Set(string lnk, string aumid) {
    Guid iid = new Guid("886d8eea-8cf2-4446-8d02-cdba1dbdcf91");
    IPropertyStore store;
    // flags=3 → GPS_READWRITE：对已存在的 .lnk 打开写属性存储
    SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 3, ref iid, out store);
    PropertyKey pk = new PropertyKey {
      fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    PropVariant pv = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(aumid) };
    store.SetValue(ref pk, ref pv);
    store.Commit();
    Marshal.FreeCoTaskMem(pv.p);
  }
}
```

`ensureShellShortcuts()` 流程：
1. `process.platform !== 'win32' || !app.isPackaged` → return；
2. `planShortcuts()`：开始菜单项按 `existsSync(startMenuLnkPath(app.getPath('appData')))` 缺失才建（每次启动检查）；桌面项按 userData 标记文件 `.desktop-shortcut-created` 不存在才建（仅首启一次，实际桌面路径由 PS 内 GetFolderPath 解析，TS 不猜）；两项都不需建 → return（零 PS 开销）；
3. `execFile(resolvePowerShell(), ['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command', script], { timeout: 15000, windowsHide: true })`，失败 `console.warn('[shortcut] 快捷方式创建失败（不影响应用运行）:', err)`；
4. 成功且本次含桌面项 → 写标记文件（异步，失败仅 warn）；全程打点 `[perf] shortcuts ensured`。

- [ ] **Step 4: main.ts 接线**

`createWindow()` 之后（whenReady 内）加：

```ts
  // tar 形态没有安装器：首启自建桌面/开始菜单快捷方式（含 AUMID，Toast 归属
  // 注册依赖开始菜单项）。fire-and-forget，失败仅 warn，绝不占用启动关键路径。
  ensureShellShortcuts().catch((e) => console.warn('[main] ensureShellShortcuts:', e))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `npx vitest run electron/shortcut-win.test.ts electron/app-user-model-id.test.ts`
Expected: PASS（守卫测试继续锁 appId↔AUMID 同源）。

- [ ] **Step 6: Commit**

```bash
git add electron/shortcut-win.ts electron/shortcut-win.test.ts electron/main.ts
git commit -m "feat(electron): tar 形态首启自建带 AUMID 的桌面/开始菜单快捷方式"
```

---

### Task 5: electron-builder.yml 收敛 + package.js tar 出件

**Files:**
- Modify: `electron-builder.yml`（L76-88）
- Modify: `scripts/package.js`（L88 之后追加 tar 步骤）

**Interfaces:**
- Produces: `npm run package` → `dist/win-unpacked/` + `dist/ckunagentssh-<version>-win-x64.tar.gz`（POSIX 命名 `<os>-<arch>`）。

- [ ] **Step 1: 改 `electron-builder.yml`**

```yaml
win:
  executableName: cKunAgentSSH
  # resources/icon.png（buildResources）— electron-builder 自动生成多尺寸 .ico
  icon: resources/icon.png
  # tar-only（2026-10-23 决策）：发布形态为解包目录直发，dir 产物即 win-unpacked/，
  # 由 scripts/package.js 压成 tar.gz。nsis/portable 已随发布策略收敛移除 ——
  # 桌面快捷方式与 AUMID 注册改由 electron/shortcut-win.ts 首启自建（见 spec §4）。
  target:
    - dir
```

删除 `portable:` 与 `nsis:` 整块；L6-21 白名单注释中 makensis 事故段保留为历史教训，但把"剔掉后包体近乎减半"一句后追加"（tar 形态下白名单同样决定 tar 体积）"。

- [ ] **Step 2: `scripts/package.js` 追加 tar 步骤**

`child.on('exit')` 回调改为：builder 退出码为 0 时，先跑 tar 再透传退出码：

```js
const version = require(path.join(root, 'package.json')).version
child.on('exit', (code) => {
  if (code !== 0) process.exit(code)
  // tar-only 出件：解包目录直发。Windows 10+ 自带 tar.exe（bsdtar），
  // POSIX 用系统 tar；找不到即失败退出——绝不出"没有发布物"的绿色流水线。
  const tarBin = process.platform === 'win32' ? 'tar.exe' : 'tar'
  const unpacked = path.join(root, 'dist', process.platform === 'win32' ? 'win-unpacked' : `${process.platform}-unpacked`)
  const artifact = path.join(root, 'dist', `ckunagentssh-${version}-${process.platform}-x64.tar.gz`)
  if (!fs.existsSync(unpacked)) {
    console.error(`[package] 未找到 dir 产物目录：${unpacked}`)
    process.exit(1)
  }
  const r = spawnSync(tarBin, ['-czf', artifact, '-C', unpacked, '.'], { stdio: 'inherit', shell: isWin })
  if (r.error || r.status !== 0) {
    console.error('[package] tar 出件失败：', r.error ? r.error.message : `exit=${r.status}`)
    process.exit(1)
  }
  console.log(`[package] tar 产物：${artifact}`)
  process.exit(0)
})
```

顶部 require 行补 `const { spawn, spawnSync } = require('child_process')`（现有 `spawn` 解构处）。

- [ ] **Step 3: 本地验证 tar 出件**

Run: `npm run package -- --allow-missing-runtime`（本机无 runtime/，用逃生口验证流水线；预计数分钟）
Expected: 末行打印 `[package] tar 产物：...tar.gz`；`tar -tzf dist\ckunagentssh-0.1.0-win32-x64.tar.gz | Select-String 'resources/icon.png|resources\\backend/main.py|cKunAgentSSH.exe'` 三类条目均在。

- [ ] **Step 4: Commit**

```bash
git add electron-builder.yml scripts/package.js
git commit -m "build: 发布收敛为 tar-only（win.target=dir + tar 出件步骤）"
```

---

### Task 6: CI 上传物切换

**Files:**
- Modify: `.github/workflows/ci.yml`（L74-77 注释、L140-141 注释、L142-149 步骤）

- [ ] **Step 1: 修改**

```yaml
      # 打包真跑一次：files/asarUnpack 规则能不能把 node-pty/ssh2 的 .node 放对位置、
      # dir 产物 + tar 出件能否成立，都只在 Windows runner 上验得了。
      - name: Package (tar.gz)
        run: npm run package

      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        with:
          name: cKunAgentSSH-windows-tar
          path: dist/*.tar.gz
          if-no-files-found: error
```

L74 注释"交付目标是 Windows-only（nsis / portable）"改为"交付目标是 Windows-only（tar 解包直发）"，"portable 首启解包"字样同步改写。

- [ ] **Step 2: 校验 YAML**

Run: `npx --yes js-yaml .github/workflows/ci.yml > $null; echo $?`
Expected: 退出码 0（js-yaml 已在 devDependencies）。

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: 发布产物切换为 dist/*.tar.gz"
```

---

### Task 7: 全量验证 + spec 实施回填

**Files:**
- Modify: `docs/superpowers/specs/2026-10-23-startup-speed-tar-packaging-design.md`（追加"实施回填"章节）

- [ ] **Step 1: 前端闸门全绿**

Run: `npx vitest run electron/ && npm run typecheck`
Expected: 全部 PASS、无类型错误。

- [ ] **Step 2: 启动性能验收（spec §1.2）**

`npm run dev *> _perf\after-a.log`（后台，等待 `[perf]` 输出后杀进程），两轮。核对：`backend: spawned ≤ 700ms`、`backend start resolved ≤ 1600ms`。未达标不得声称完成，回到 §1.1 基线找新的串行点。

- [ ] **Step 3: tar 解包冒烟（人工步骤，向用户确认后执行）**

解压 Task 5 产物到临时目录 → 启动 `cKunAgentSSH.exe` → 检查：托盘出现猫形 C 图标；桌面与开始菜单出现 cKunAgentSSH 快捷方式且图标正确；设置页开"桌面通知"后失焦触发一次通知，横幅归属显示 cKunAgentSSH。

- [ ] **Step 4: spec 回填 + 提交**

在 spec 末尾追加 `## 7. 实施回填（2026-10-23）`：实测前后对比表（基线取 §1.1，新值取 Step 2）、与设计的差异及原因（如缓存失效判定口径调整等，没有差异也写"无"）。

```bash
git add docs/superpowers/specs/2026-10-23-startup-speed-tar-packaging-design.md
git commit -m "docs(spec): 启动提速与 tar 收敛实施回填"
```
