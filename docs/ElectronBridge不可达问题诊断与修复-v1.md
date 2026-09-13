# Electron Bridge 不可达 —— 诊断与修复报告 v1

日期：2026-09-13
现象：定时任务执行失败，日志报 `agent.electron_bridge.BridgeError: [BRIDGE_PORT_CLOSED] port=30000`，
且主进程日志明确打印 `[ElectronBridge] listening on 127.0.0.1:30000`（端口"明明在监听"）。

---

## 一、结论先行

**Electron 主进程监听的 30000 端口处于"绑定成功但入站不可达"状态。**

根因：**本机 Windows 防火墙对回环（127.0.0.1）TCP 入站连接按可执行文件放行，而
`electron.exe` 没有任何防火墙规则**，因此其监听端口无法被连接。

后端探活超时 → 归类失败 → 定时任务全部中断。这与"端口未监听"是两回事，日志措辞
（`PORT_CLOSED`）把排查方向带偏了。

---

## 二、关键证据

### 2.1 端口确实在监听，但 TCP 握手超时

| 目标端口 | 监听进程 | 实测 TCP connect |
|---|---|---|
| 8010 | `D:\ProgramData\miniconda3\python.exe` | **1 ms 成功** |
| 5173 | `C:\Program Files\nodejs\node.exe` | **1 ms 成功** |
| 30000 | `node_modules\electron\dist\electron.exe` | **超时（4 s / 8 s 均超时）** |

- `netstat -ano`：`TCP 127.0.0.1:30000 0.0.0.0:0 LISTENING 34212`
- 无任何 ESTABLISHED 连接堆积（排除 backlog 满）
- 主进程存活且**事件循环健康**：CPU 采样 4 次 delta 均为 0，48 线程中 33 个处于
  正常 `UserRequest` 等待（排除主进程死锁）

### 2.2 对照实验（决定性）

同一脚本、同一端口、同一时刻，仅替换解释器：

| 解释器 | 防火墙 Allow 规则 | 绑定 30001 后自连 |
|---|---|---|
| `.workbuddy\binaries\python\versions\3.13.12\python.exe` | 有 | **True** |
| `.conda\envs\ai\python.exe` | 有 | **True** |
| `.workbuddy\binaries\node\versions\22.22.2-3\node.exe` | **无** | **False** |
| `node_modules\electron\dist\electron.exe` (30000) | **无** | **False** |

规则清单核对：`electron.exe` 在防火墙中**无任何条目**（`NONE - electron.exe has no firewall rule`）。
7 项观察全部与"按程序放行回环入站"一致。

### 2.3 已排除的原因

| 假设 | 排除依据 |
|---|---|
| HTTP 代理劫持 | 后端进程 env 实测**无** `HTTP_PROXY`/`HTTPS_PROXY`（仅 `npm_config_noproxy` 空值）；且 httpx 已加 `trust_env=False` |
| 沙箱网络限制 | 沙箱内外结果一致；同环境 8010/5173 均可达 |
| 防火墙显式 Block 规则 | 无 electron 相关 Block 规则 |
| Windows 保留端口段 | `netsh` 排除范围为 50000-50059 等，**不含** 30000-30100 |
| accept 队列积压 | 无 ESTABLISHED 连接堆积 |
| 主进程死锁 | CPU/线程采样显示事件循环空闲健康 |
| token 不匹配 | 用后端真实 token 请求仍超时 |

### 2.4 附带发现（独立缺陷，已一并修复）

1. **孤儿后端清理逻辑从未生效**
   `tasklist /FO CSV /NH` 输出**不含命令行**，`filter(includes('main.py'))` 恒为空，
   必然落入 `wmic` 回退分支；而 `wmic` 已从 Win11 移除（实测 `'wmic' 不是内部或外部命令`）。
   实测残留一个 4 小时前的孤儿 `python.exe`（pid 9340）。

2. **沙箱遗留回环屏蔽规则（需留意）**
   防火墙存在 `codex_sandbox_offline_block_loopback_tcp`：
   `Enabled=True, Outbound, Block, Program=Any, Remote=127.0.0.0/8`。
   该规则一旦生效会阻断**整机所有**程序到 127.0.0.1 的 TCP 连接。
   实测当前未生效（有 Allow 规则的进程回环正常），但属于高风险项，建议定期核查。

3. **探活端点语义错位**
   后端原先用 `/ssh/exec` 发空参数探活——该请求会被参数校验拒成 400，而 400 又被当作
   "桥存活"，探活与业务逻辑耦合；且所有失败一律报 `PORT_CLOSED`。

---

## 三、修复清单

### 3.1 代码修复（已完成）

**`electron/electron-bridge.ts`**
- 新增启动自检 `probeSelf()`：listen 回调只证明系统调用成功，**不证明端口可达**；
  启动时主动回环 connect 探测，不可达则换端口重建（此时后端 env 尚未注入，换端口安全）
- `findFreePort()` → `listenFirstFree()`：删除"预探测→close→重新绑定"的两段式流程，
  改为"绑定失败即换下一个端口"，消除 Windows 上的绑定竞态
- **降级而非抛异常**：自检全部失败时返回端口并打出明确告警。
  若在此抛异常，`main.ts` 的 catch 会连带跳过 Python 后端启动，把局部故障放大为全局不可用
- 新增 `/ping` 专用健康端点（不触碰业务逻辑，仍需 token）
- 请求全链路日志：`REQ`（请求到达）/ `RESP`（响应状态+耗时）写入 `electron-bridge.log`，
  用于区分"请求没到（网络层）"与"处理挂起（应用层）"
- 运行期每 60 s 自检一次，端口中途失效时留痕
- `_secureCompare` 改用顶部 import 的 `timingSafeEqual`，去掉 `require('crypto')`

**`backend/agent/electron_bridge.py`**
- 新增 `probe()`：返回 `BridgeErrorCode | None`，区分 TIMEOUT / PORT_CLOSED /
  TOKEN_REJECTED，不再一律归为 PORT_CLOSED；`ping()` 保留为布尔薄封装
- 所有请求改用 `trust_env=False`：桥接是纯回环 IPC，绝不能受代理环境变量影响

**`backend/agent/schedule_engine.py`**
- 探活改用 `probe()`，把**真实**失败原因码写入 run summary

**`electron/backend-manager.ts`**
- 孤儿检测改用 PowerShell CIM 查询 `Win32_Process.CommandLine`（不依赖已移除的 wmic）
- 新增 `isOurBackendProcess()` 二次甄别：要求命令行含本应用 backend 目录，或同时含
  `--host 127.0.0.1` 与 `--port`，避免误杀其他以 `main.py` 为入口的服务

### 3.2 环境修复（需管理员执行一次）

```powershell
# 以管理员身份运行
pwsh -File scripts\fix-electron-firewall.ps1
```

脚本对 `electron.exe` 添加**仅限回环**的入站允许规则：
`-Direction Inbound -Action Allow -Protocol TCP -LocalAddress 127.0.0.1 -RemoteAddress 127.0.0.1`

---

## 四、验证方法

重启应用后：

1. `electron-bridge.log` 应出现 `self-probe OK on 127.0.0.1:<port>`；
   若仍失败会出现 `self-probe FAILED` 与 `UNREACHABLE ...` 及其原因提示
2. 手动确认端口可达：
   ```powershell
   (Test-NetConnection 127.0.0.1 -Port <bridge端口>).TcpTestSucceeded
   ```
3. 触发一次定时任务，确认不再出现 `[BRIDGE_PORT_CLOSED]`

---

## 五、遗留风险

- 打包模式下可执行文件路径变为 `dist\win-unpacked\cKunAgentSSH.exe`，
  防火墙规则需按新路径追加（脚本已内置该候选路径）
- `codex_sandbox_offline_block_loopback_tcp` 若被启用，会再次阻断全部回环通信
- 彻底摆脱防火墙依赖的演进方向：将桥接从 TCP 回环迁移到 Windows 命名管道
  （命名管道不经 WFP 网络过滤），代价是 Python 侧需新增 named pipe 传输层

---

## 附录：第二轮修复（用户重启验证后暴露）

首轮修复经实机验证生效——日志出现 `self-probe` 记录，应用在桥接不可用的情况下
**正常降级启动**（后端、前端 API 全部正常）。但暴露出两个自身缺陷：

### A. 换端口重试原地空转

现象：三轮日志全部是 `listening on 127.0.0.1:30000`。

原因：`destroyServer(server)` 释放端口后，下一轮又从 `PORT_RANGE_START` 开始绑定，
于是重新拿到同一个 30000 —— "换端口重建"实际是原地重试。

修复：
- 新增 `nextStartPort`，自检失败后递增（`port + 1`），确保真的换端口
- 已到区间末尾（`port >= PORT_RANGE_END`）则直接结束循环，不做无意义重试
- `START_MAX_ATTEMPTS` 由 3 降为 2：换端口只能绕过"单端口幽灵"，
  对"按可执行文件拦截回环"这类与端口无关的成因完全无效，多试只是白白拖慢启动
  （每轮自检超时 1.5 s）。2 轮足以区分这两类成因

### B. CIM 孤儿检测仍失败

现象：`[BackendManager] Cannot detect orphan processes (CIM query unavailable)`。

排查：直接执行 `Get-CimInstance Win32_Process` 查询**完全正常**，返回
`pid=31692 cmd=python main.py --port 8010 --host 127.0.0.1`。`Winmgmt` 服务 Running，
`powershell.exe` 存在。

原因：`execFileSync('powershell', ...)` 依赖 `PATH` 解析可执行文件，而该应用是从
**PATH 被裁剪的环境**启动的——同一个父 shell 连 `ls` / `dirname` 都找不到
（本会话实测）。于是 spawn 裸名 `powershell` 直接 ENOENT。原 `catch {}` 把它静默
吞成一句 "CIM query unavailable"，掩盖了真实根因。

修复：
- 新增 `resolvePowerShell()`：用 `process.env.SystemRoot`（回退 `WINDIR`）拼出
  `System32\WindowsPowerShell\v1.0\powershell.exe` 绝对路径，并兼容 `Sysnative`
  别名路径；找不到时才回退裸名
- `catch` 中打印真实异常消息，便于区分"无 PowerShell / 无 WMI / 权限不足 / 语法错"

### 状态

代码侧修复完成，`tsc -p tsconfig.node.json --noEmit` 与 `py_compile` 均通过。
**功能恢复仍待环境侧放行**：当前会话无管理员权限（令牌未提权、`UAC EnableLUA=1`、
写防火墙策略被拒）。若无法提权，则采用方案 B：桥接迁移到 Windows 命名管道。
