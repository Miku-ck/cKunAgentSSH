# S4 · SSH 远程终端与连接管理

锚点 `a8e588c`。范围：`electron/ssh-manager.ts:1-1236`（会话/重连/known-hosts，SFTP 段 1237-2089 归 S5）/ `pty-manager.ts`(262) / `ssh-store.ts`(412) / `utils/path-guard.ts`(114) / `main.ts` ssh 组 1082-1181 + pty 组 894-918 / 渲染层 `SshView.vue` `RemoteTerminal.vue` `LocalTerminal.vue` `RemoteManager.vue` `ConnectionModal.vue` `ConnectionRow.vue` `stores/ssh.ts` `useSshNavigation.ts` `useTerminalConfig/Context` `useTabContextMenu` / 快捷命令全套（`quickCommands.ts` `QuickCommandBar/Editor.vue` `BulkCommandModal.vue` `qc-align.ts` `bulkCommandDiff.ts`）。

**本片无 spec**（`docs/` 下无 SSH 终端规格）→ 规格轴只对照 `docs/security-boundary.md` 的边界声明。**本片行号已逐文件核验 working-tree == a8e588c**（`git diff a8e588c --stat` 对全部读列文件返回空），故与并行会话的 21 个脏文件无冲突。

## 缝表

| 渲染层符号 | 通道/端点 | 处理器 | 判定 |
|---|---|---|---|
| `env.d.ts:127-160` 的 5×`ssh:*` + 2×`pty:*` push 通道 | `ssh:data/close/reconnect/host-key-new/host-key-mismatch`、`pty:data/exit` | 生产端 `ssh-manager.ts:713 / :533 / :1024,1059,1078,1128 / :492 / :497`，`pty-manager.ts:204 / :211` | ok — 7 条载荷形状全部与 `env.d.ts` 声明一致，消费端 `RemoteTerminal.vue:328,337,354`、`LocalTerminal.vue:198,206`、`stores/ssh.ts:101,107`、`SftpView.vue:598`、`useAgentDeliver.ts:102-103`；除 `useAgentDeliver`（`:93` 注释明写刻意常驻）外全部在卸载时摘除 |
| `api.sshWrite` / `api.ptyWrite` | `ssh:write` / `pty:write` | `main.ts:1108-1110` / `:898-900` | **契约错位候选** — 处理器返回 void ⇒ `invoke` 恒 resolve ⇒ 调用方的 `.catch` 永不可达（S4-06） |
| `api.sshConnect` | `ssh:connect` | `main.ts:1082-1104` → `ssh-manager.connect:600` | 载荷多带 `note/environment/tags/hasPassword/hasPassphrase/createdAt/updatedAt`，`env.d.ts:129-135` 未声明 → `buildConnectConfig:540-597` 忽略，结构化克隆安全 ⇒ **良性**，不作发现 |
| `sshCompleteCommand.prevSegments` | `pty:completeCommand` | `env.d.ts:189` 必填 vs `preload.ts:190` 可选，`main.ts:262` 有默认 | 省略无害 ⇒ **良性** |
| `api.sshExec(id, cmd, timeoutSec?)`（`env.d.ts:150`） | `ssh:exec` | `main.ts:1133-1136` → `execCommandWithExitCode:844-926` | 三参签名与默认/上限一致；但**该通道完全绕过后端审批与审计**（S4-07） |
| `ssh:saveConnection` / `listConnections` / `testConnection` | 同名 IPC | `main.ts:1151-1154` / `:1144-1149` / `:1161-1202` | `stripCredentials:1059-1062` 一致；**凭据持久化失败无用户面**（S4-03） |
| `client.connect` 未包 try（`ssh-manager.ts:784`） | — | — | 已在 **S0-12** 立案，本片不重复计数 |

## 发现表

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S4-01 | c | Major | ✅ | PTY shell 白名单按 **basename 后缀**放行，任意目录下的同名可执行文件都能起 | `pty-manager.ts:113-115`（表 `:28-35`） | 恒（判定即如此），触发需受控渲染进程 |
| S4-02 | c | Major | ✅ | `pty:completeCommand` 对渲染层给的目录做枚举/`cd`，**是唯一不调 `assertSafePath` 的文件原语** | `main.ts:918-1019` + `listFilesByPrefix:369-382` | 无需攻击即可观察（TAB 补全 `~/.ssh/`），边界失守面在受控渲染进程 |
| S4-03 | c | Major | ✅ | safeStorage 不可用的机器上密码**静默存不进**，`ssh-store.ts:49-50` 写明的「调用方须先检查并提示改用私钥」契约无人实现 | `ssh-store.ts:46-54` + `:246-248`，`main.ts:1151-1154` | Linux 无 Secret-Service（win32/darwin 走 DPAPI/keychain 不触发） |
| S4-04 | a | Major | ✅ | 自动重连对**确定性不可重试**错误照跑 12 次/≈4.7 分钟：12 次认证尝试撞 sshd MaxAuthTries，主机密钥变更则堆 12 条永不消失的「中间人攻击」toast | `ssh-manager.ts:1072-1084`（catch 不判错误）↔ `shared/ssh-error-core.ts:54-66` | 高：任何意外断连都进这个循环 |
| S4-05 | a | Major | ✅ | `connectRemote` 无在途互斥 + debounce 路径的 `releaseConnectLock` 解除**自己那笔仍在跑**的锁 → 同一主机两条并发会话 | `useSshNavigation.ts:213-233`，入口 `SshView.vue:129-141`、`RemoteManager.vue:104→:379→:265` | 中高：握手慢的主机 + Quick Start 卡片（等待期毫无反馈）间隔 >2s 点两次 |
| S4-06 | a | Major | ✅ | 重连窗口内点快捷命令**静默丢失**（不敲键、不报错、重连成功后也不补发），而那条错误 toast 是不可达死码 | `QuickCommandBar.vue:110-114` ↔ `main.ts:1108-1110`、`ssh-manager.ts:786-791`、`pty-manager.ts:217-223` | 高：网络抖动期间点一下即现 |
| S4-07 | b | Major | ✅ | 批量下发走 Electron 侧 `ssh:exec`，**零审计记录** —— 与 `security-boundary.md:26,138-139` 宣称的「批量通道在边界内、靠事后审计兜底」直接矛盾（09-21 已列**待裁决**，非已驳回） | `BulkCommandModal.vue:279` → `main.ts:1133-1136`；`grep -rni audit electron/` = 0 命中；`/agent/safety/classify`（`routes/agent.py:1499-1515`）自述「只读无状态」 | 恒（每次批量执行） |
| S4-08 | a | Minor | ✅ | 补全会话清理跑在 `willReconnect` 提前返回**之前**，把刻意保留的会话的状态删了 | `RemoteTerminal.vue:340` vs `:342-347`；`useCompletion.ts:512-518` | 窄：断开瞬间恰有 Tab 补全在途（窗口一个 IPC 往返） |
| S4-09 | a | Minor | ✅ | `doConnect` 先无条件写会话池、代数校验在其后 ≈一次握手之外（**当前 UI 无触发路径**） | `ssh-manager.ts:640-671` vs `:1090-1100` | 不可达（已找遍入口），登记为唯一的「守卫前置失败」位点 |
| S4-10 | d | Minor | ◐ | `qcHeightForAlign` 的不变量测试只用整数基线，亚像素情形只断言返回值不断言不变量 | `qc-align.ts:33-34,42-53` ↔ `qc-align.test.ts:26-34,42-45` | 观感（1px），DOM 侧不可静态证 |

## 详情

### S4-01 [dim c][Major] M3 白名单的判定面是 basename，不是位置
- LOCATION：`electron/pty-manager.ts:113-115` —
  ```ts
  const isAllowed = allowed.some(s => shellBase === s.toLowerCase()
    || shellBase.endsWith('/' + s.toLowerCase())
    || shellBase.endsWith('\\' + s.toLowerCase()))
  ```
  条目表 `:28-35`（win32 全部是裸名 `cmd.exe`/`powershell.exe`/`wsl.exe`…）。
- CLAIM：`D:\任意可写目录\cmd.exe` 命中 `endsWith('\\cmd.exe')` 放行；等值分支还接受**相对名** `cmd.exe`，实际解析到哪由渲染层传的 `cwd` 决定，而 `cwd` 只经 `:160-168` 的 `statSync` 目录存在性校验，没有任何 root/containment 检查。紧随其后的参数白名单 `:136-156` 也按同一 basename 分支，所以一个改名成 `powershell.exe` 的植入体还能顺带领到 `-NoLogo -NoExit`。`:27` 的注释把这个判定说成「防止渲染进程启动任意命令」——它就是那道边界，且是唯一一道。对照同仓另一处凭据防护 `assertKeyFileSafe` 是真的用 `isInside` 做位置约束的，可见项目并非没有「按位置约束」的写法。
- 可达性：威胁模型是项目自设的（`main.ts:1286` 等处的「受控渲染进程」口径），落文件的原语 `fs:mkdir:687` / `fs:writeFile:484` 只挡敏感路径。合法 UI 不受影响——`ssh.ts:152-199 resolveShellPath` 一贯送绝对候选路径，收紧到已知安装根（System32 / Git 安装目录 / `/bin`）零成本。
- 候选修复方向（未实施）：win32 分支改「必须位于 `process.env.SystemRoot\System32` 或已探明的 Git 安装根之下」，并拒绝对 `shell` 做相对名解析。

### S4-02 [dim c][Major] 补全通道是 fs 原语防护表上的洞
- LOCATION：`main.ts:918-1019`。win32-CMD 分支 `:932-941` → `listFilesByPrefix(dir, filePrefix)`（`:369-382`，裸 `readdirSync`）；POSIX 分支 `:1014` `execFile('bash',['-lic',…],{cwd: actualCwd})`，脚本体内 `compgen -A file`（`:299`）；PowerShell 分支 `:977-980` 同样 `cwd: actualCwd`。`dir/actualCwd/prefix/shell` 全部来自渲染层载荷（`:920` 解构，`:922` 还让渲染层决定走哪个 shell 分支）。
- CLAIM：三条分支都**没有** `assertSafePath`。于是 `~/.ssh`、`~/.aws`、`~/.kube` 下的**条目名**能经 IPC 回到渲染层——而这正是 `ANYWHERE_SENSITIVE`（`path-guard.ts:23-25`，`'.ssh','.aws','.gnupg','.kube'`）存在的目的（`:64-68` 的任意段拦截）。同文件里其余每个列举/stat 原语都做了防护：`fs:readDir:425`、`fs:readFile:465`、`fs:stat:790`、`fs:exists:784`、`sftp:upload/download:1289,1293` —— 全仓 `assertSafePath` 共 24 个调用点，独缺这三条。
- 可达性：诚实区分两层：正常用户敲 `cd C:\Users\me\.ssh\i<TAB>` 就能看到文件名，**这条路径本身没泄露任何他本来看不见的东西**，所以真实危害为零；缺陷是「边界写在了文档与 24 个调用点上，第 25 处漏了」，受控渲染进程/XSS 可以在没有人在场的情况下直接调它枚举。故定 Major 而非 Critical。
- 佐证：`grep -n "assertSafePath" electron/main.ts` 命中 24 处（`:425,465,486,504,551,626,640,688,694,695,701,702,773,784,790,808,809,854,855,1289,1293`），无一在 `pty:completeCommand` 内。

### S4-03 [dim c][Major] 加密不可用 = 密码永久存不进，且没有任何一层告诉用户
- LOCATION：`ssh-store.ts:46-54`（`encrypt()` 在 `!isEncryptionAvailable()` 时返回 `''` 哨兵，注释 `:49-50` 明写「调用方应在保存前检查 isEncryptionAvailable 并提示用户改用私钥认证」）→ `:246-248`（`doSave` 确实检查了，但只 `console.warn`）→ `fromDisk:106` 经 `decrypt('')`→`:57 if (!stored) return undefined` 得到 `password: undefined`。调用方 `main.ts:1151-1154`（`ssh:saveConnection`）**不检查**；渲染层也**无从检查**——旧 `auth:isEncryptionAvailable` IPC 已在 `main.ts:1253-1255` 的「审查清理」里删除（注释自述「渲染层零调用」），`grep -rn "isEncryptionAvailable\|safeStorage" src/` 只剩 `stores/ssh.ts:3` 一句注释。
- CLAIM：保存成功、下次连接即失败。要修正一处**当年的描述**：`doSave` 的返回值在 `:294-299` 已改为「以合并后的磁盘记录 `fromDisk(disk)` 为准」，因此 `stripCredentials` 算出的 `hasPassword` 会**如实报 false**，编辑弹窗也不会谎称「已保存」——这一点当年审查修对了。剩下的真实缺陷是：用户按提示输过密码、点保存、无警告，回来发现连接因「密码认证失败」起不来，而那条失败的文案（`main.ts:1026-1031` 的 USERAUTH_FAILURE 说明）不含「本机安全存储不可用」这个唯一可行动的根因；重输多少次都没用。
- 可达性：Linux 打包/dev 且无 Secret-Service provider（`isEncryptionAvailable()===false` 的文档化场景）。win32/darwin 由 DPAPI/keychain 兜住，**不影响主力平台** → 若你判定 Linux 出界，可降 Minor。

### S4-04 [dim a][Major] 重连循环无视仓库里既有的 retryability 口径
- LOCATION：`ssh-manager.ts:1072-1084` —
  ```ts
  } catch (err) {
    if (!this.isReconnectCurrent(id, entry, gen)) return
    entry.inFlight = false
    console.warn(`[ssh] 重连第 ${attempt}/${RECONNECT_MAX_ATTEMPTS} 次失败:`, …)
    if (attempt >= RECONNECT_MAX_ATTEMPTS) { …'exhausted' } else { this.scheduleReconnectAttempt(id, attempt + 1) }
  ```
  全程不读 `err` 的类型/code/retryable。退避与次数：`:354-365`（`RECONNECT_MAX_ATTEMPTS=12`，`reconnectDelayMs = min(1000·3^(n-1), 30_000)` ⇒ 累计 ≈4.7 分钟），排程 `:1018-1036`。
- CLAIM：仓库已有权威可重试判据 `isRetryable(code)`（`shared/ssh-error-core.ts:54-66`：只有 `CONN_REFUSED/CONN_TIMEOUT/CONN_RESET/NETWORK_ERROR/EXEC_TIMEOUT/SFTP_FAIL` 为真），`SSHError` 甚至自带 `.retryable`（`electron/ssh-error.ts:84`），且 `ssh-manager` 已经 import 了 `ssh-error.ts:14`。但 `HOST_KEY_MISMATCH`、`AUTH_PASSWORD/AUTH_KEY/AUTH_AGENT`、`DNS_FAIL`、`SESSION_LIMIT` 在这些判据下全是**不可重试**，重连循环却一律跑满 12 次。两个具体后果：① 对远端 sshd 连发 12 次认证尝试（MaxAuthTries / fail2ban / 账户锁定风险）；② 密钥轮换或真 MitM 时，`:484-499` 的 `verifyHostKey` 每次尝试都 `safeSend('ssh:host-key-mismatch')`（`:497`），渲染层 `stores/ssh.ts:107-112` 的 handler 用 `toast.error(msg, 0)`，而 `toast.ts:51-54` 的 `if (duration > 0) armDismiss` 使 duration=0 永不自动关闭 ⇒ **12 条叠着的「可能存在中间人攻击」告警**。`config-missing` 已有快速中止分支（`:1056-1063`），说明「按原因决定要不要继续重连」是本文件既有的设计意图，只是安全/认证类错误没走它。
- 可达性：高。任何意外断连都进这个循环；密钥轮换场景下 ①②同时命中。
- 佐证：`verifyHostKey` 在 ssh2 拒绝之前触发（`node_modules/ssh2/lib/client.js:281-287` 的 `hostVerifier` 包装同步调 `verify(ret)`）；catch 块内 `grep retryable` 零命中。

### S4-05 [dim a][Major] 「同一次连接」的锁可以被它自己解掉
- LOCATION：`useSshNavigation.ts:213-233`；入口 `SshView.vue:129-141`（Quick Start 卡片 `@click="connectRemote(conn)"`，卡片不渲染 `connecting` 态）、`RemoteManager.vue:104 → :379 → :265-267`（右键「连接」）、`useSshNavigation.ts:298-304`（`handleSaved` 的 connectNow）。
- CLAIM：两个缺陷、一个后果。
  1. **没有在途互斥**：`connectRemote` 只在 `:222` 比 `CONNECT_DEBOUNCE_MS`（`:15` = 2000ms），`connecting.value = true`（`:232`）纯属显示用，进入时不判。而 `readyTimeout` 默认 15000ms（`ssh-manager.ts:545`）——首连仍在 await 时，Quick Start 浮层不会消失（`showHome.value = false` 只在成功分支 `:240`），卡片也没有转圈，于是 >2s 后的第二次点击真的再开一条会话（`stores/ssh.ts:263-270` 的 `connectRemote` 两次都 push）。`MAX_SESSIONS=20`/`SESSION_LIMIT=64` 挡不住 2 条。
  2. **debounce 分支解除的是自己的锁**：`releaseConnectLock(conn.id)`（`:225`）的守卫是 `if (pendingConnId.value && pendingConnId.value !== connId) return`——只有 `pendingConnId === connId` 时才往下清。而 `pendingConnId === connId` 恰恰意味着**同一连接还有一笔在途**，此时清掉 `connecting/pendingConnId` 正是函数自己 `:209-212` 注释禁止的事（那段注释管的是「A 结束越权清 B」，这里是「A 的第二次点击清掉 A 自己」，同一不变量的另一半没被守住）。后果：`ConnectionRow.vue:70 isConnecting` 翻回 false，`:341-343` 的 `.loading{pointer-events:none}` 消失，仍在连接的那一行重新可点。
- 可达性：中高。握手慢/认证慢的主机上，用户在毫无反馈的等待期多点一次即成。

### S4-06 [dim a][Major] 快捷命令在断连窗口静默吞掉，失败提示是不可达死码
- LOCATION：`QuickCommandBar.vue:110-114` —
  ```ts
  const writer = session.type === 'local' ? api.ptyWrite : api.sshWrite
  writer(sessionId, cmd.command).catch((err) => { console.error(…); toast.error('命令发送失败') })
  ```
  处理器 `main.ts:1108-1110`（`ssh:write`）与 `:898-900`（`pty:write`）都是同步 void；`ssh-manager.write:786-791` 是 `if (session?.stream)`，`pty-manager.write:217-223` 是 `if (session)`。
- CLAIM：会话不在时两个 manager 都**静默 no-op**，void handler 经 `handleTrusted`（`main.ts:57-76`，同步结果原样返回）成功 resolve ⇒ `ipcRenderer.invoke` 永不 reject ⇒ `:111-114` 那条 `.catch` 与「命令发送失败」toast **没有任何触发路径**。自动重连窗口内（1s…30s，最多 12 次 ≈4.7 分钟，`ssh-manager.ts:354-365`）渲染层 `store.sessions` 仍持有该 tab、`showCommandBar` 仍为真（`useSshNavigation.ts:33-35`），于是主进程已无会话而命令条是活的：点击无按键、无回显、无报错，命令连重连成功后也不会补发——彻底丢失。同一 swallow 也作用于打字输入（`RemoteTerminal.vue:393-398`），但那是天然的连续流、不可见；快捷命令是**一次离散用户动作 + 一条写着但走不到的失败路径**，性质不同。对照 `execCommandWithExitCode:849-850` 是会抛 `SESSION_NOT_FOUND` 的，可见「按会话存在性抛错」在本文件也是既有做法。
- 可达性：高。网络抖动 → 看到「重连中 (n/12)」→ 点一条快捷命令，即现。

### S4-07 [dim b][Major] 批量下发通道不在审计链里，而安全边界文档把话说反了
- LOCATION：行为链 `BulkCommandModal.vue:279 api.sshExec(s.id, cmd, timeoutSec)` → `main.ts:1133-1136` → `ssh-manager.execCommandWithExitCode:844-926`。文档侧 `docs/security-boundary.md:26`（表格行：「人自己敲错命令 | 交互 PTY 不在边界内（见 §5）；**批量下发在（hardline 中止）** | **事后审计**」）与 `:138-139`（「替代防线是：① 事后审计（链式签名，谁在什么环境执行了什么**都可追**）；…③ 真正有乘数效应的批量通道本轮补了 hardline」）。
- CLAIM：前半句成立、后半句不成立。批量弹窗确实做了强制的后端判定往返（`BulkCommandModal.vue:210-248` → `agentApi.classifyCommands` → `routes/agent.py:1499`，`bulkCommandDiff.ts:77-104` 在 `verdict===null` 时 fail-closed、hardline 阻断、dangerous/prod 需确认，且没有本地规则表——这部分实现得好）。但它**不落审计**：`grep -rni audit electron/ --include=*.ts` 零命中，`ssh:exec` 腿不经过 PermissionGate 也不经过 `audit.append_record`；而那个被用来「兜底」的 `/agent/safety/classify` 端点自己在 docstring `:1501-1515` 声明「只读无状态」。于是 §5 用「事后审计可追」作为「人工通道不加事前阻断」的前提（`:156`），对批量这条**有乘数效应的通道**是空的——一次向 8 台生产机下发 `systemctl restart`，事后在审计链里查不到任何痕迹。
- 状态澄清（重要）：这条**不是**已驳回项。它在 `agent-architecture-audit-2026-09` 的 09-21 记录里被明确列为「**待用户裁决**（除死锁外新增）：… 批量 sshExec 不落审计（与 security-boundary.md『都可追』相反）」，本轮复核**逐字仍成立**，故在本战役总表里重新登记、等一次裁决，不视为重复发现，也不援引「勿重议」豁免。
- 候选修复方向（未实施，二选一即可）：① 批量弹窗在派发后把每条 `(host, command, exitCode)` 经既有 `/agent/audit` 写入口落一条 `conclusion=executed`；② 或者把 `security-boundary.md:26,138-139` 的措辞改成「批量通道仅有事前 hardline，无事后审计」，别让文档继续承诺一个不存在的防线。

### S4-08 [dim a][Minor] 清理顺序：先删状态，再决定这个会话其实要留着
- LOCATION：`RemoteTerminal.vue:337-351` — `:340` 无条件 `releaseCompletionSession(props.sshId)`，`flushBuffer(true)` 之后 `:342-347` 才 `if (willReconnect) return`（该分支自己的注释写着「保留 tab…xterm 实例（scrollback 保留）」）。`useCompletion.ts:512-518` 的契约是「会话**关闭**时清理」。
- CLAIM：对同一个**存活**会话删掉了 `_completionRequestIds[sshId]`（`:371`）与 `_aiPredictInFlight`（`:392-393`）。两点后果：① 请求序号从 0 重启，断连前那条仍在途的补全响应回来时 `requestId === _completionRequestIds.get(id)`（`:499`）反而被判为「最新」，把陈旧候选列表渲染成对新一次 Tab 的应答；② AI 预测的在途闸门被清，正是 `:333`、`:343-345` 那个 Set 要拦的「双发 LLM 请求」被重新打开。
- 可达性：窄（窗口只有一个 IPC 往返），当前无用户可见症状 ⇒ Minor。与 91 清单已裁决的 `destroySession`/`openSftp` 迟到回调不同族——这是**新增**的一个晚回调位点，不构成重议。

### S4-09 [dim a][Minor，当前不可达] 会话池写入早于自己的代数守卫
- LOCATION：`ssh-manager.ts:640-671`（`ready`/shell 成功后无条件 `this.sessions.set(id, …)`）↔ `:1090-1100`（`isReconnectCurrent` 只在 `doConnect` resolve 之后评估）。
- CLAIM：身份/代数守卫比它所保护的池写入晚约一整次 TCP+认证+开壳时间（上限 `readyTimeout`=15s）。若同一 id 的两轮重连真能重叠，后写入者覆盖池条目，落败者的 `teardownSession(id, ownClient)` 会删掉**赢家的**池记录 ⇒ 一个仍向渲染层发 `ssh:data` 的活 client，其 `write/exec/resize` 全部失效，且 `close` 永远发不出（tab 看起来连着但实际失聪）。
- 我按你要求找过触发路径，**没有找到**：`reconnect()` 要求先处于 gave-up（`:989-997`），渲染层只在 `reconnectState` 为 null 时渲染重连按钮（`RemoteTerminal.vue:5-14`、`:357-389`），`startAutoReconnect` 只能从池中存在会话触发（`:523-537`），而渲染层 `connect()` 恒分配新 id。登记理由：这是本文件唯一「先改池、后自证身份」的晚回调位点，未来任何「重试期间允许再点重连」的改动都会直接落在这里。
- 顺带核实并判定**无发现**的一项：`nextId` 每次主进程启动从 1 重启（`:389`；`pty-manager.ts:63` 同），所以 `ssh-1` 重启后会被重新发出。但它不可能指错对象——渲染层会话态纯内存（`grep -rn "localStorage.setItem" src/` 只有 chat/settings/quickCommands，无 `sessions`/`activeSessionId`），主进程重启必然重建 BrowserWindow（`main.ts:1355-1365`，单实例锁 `:1342`），`ssh-connections.json` 只持久化 `conn-<ts>-<rand>` 配置 id（`ssh-store.ts:208-210`），从不存会话 id。镜像场景（渲染层重载 → 主进程留孤儿会话，因无 `ssh:listSessions`/对账 handler）同样不可达：`main.ts` 里没有 `webContents.reload`、没有 `before-input-event`、没有 `render-process-gone`、也没有 application menu（四处 grep 全空），只有 devtools 手动重载能触发。

### S4-10 [dim d][Minor] 亚像素不变量没被它自己声称的那类输入覆盖
- LOCATION：`qc-align.ts:33-34`（注释声明不变量）与 `:42-53`（`round(baseBottom) - (round(containerBottom) - 1)` 各操作数**各自**取整）；测试 `qc-align.test.ts:26-34` 的不变量循环用固定整数 `BASE = 1000`，`:42-45` 的亚像素用例只断言返回值 33，不断言 `qcTop === containerBottom - SEPARATOR_PX`。
- CLAIM：调用方用**未取整**的基线定位 QC，所以「QC 上沿与分隔线上沿同 y」只在 `baseBottom` 为整数时成立；而测试文件 `:42` 的注释自己说「DPR 缩放后 rect 常带小数」，恰好是未被覆盖那一类。1px 观感，且实际两个 rect 可能带同一小数 ⇒ 定 Minor、且我**未能**从源码证实 DOM 侧真会错开（标 ◐）。与当年已修的「±1 方向反了」不是同一件事，不重议。

## SSHManager 会话 API 档案（供 S5/S6/S7/S12 直接引用，勿重读 2089 行）

生命周期注入：`setConfigProvider(fn(connectionId)=>Promise<cfg|null>)` `:406-408`（main.ts:1074-1076 注入 `SSHStore.get`）· `setOnReconnected(fn(connectionId))` `:411-413`（main.ts:1078-1080 → `touchLastConnected`）· `setWindow(win)` `:415-417`（每次 createWindow 重申，main.ts:152-153）· `saveKnownHosts()` `:458-469`（串行在 `savePromise` 上，快照在 `then` 内即写时生成）。

公开面：`connect(config,cols,rows)` `:600-606`（分配 `ssh-${nextId++}`，返回体剥掉 `client`）· `connectSftp(config)` `:609-613`（分配 `sftp-${nextId++}`，`withShell=false`）· `write(id,data)` `:786-791`（**会话缺失即 no-op**，见 S4-06）· `resize(id,cols,rows)` `:794-814`（仅会话存在时更新 `cols/rows` 与重连条目 ⇒ 重连窗口内的 resize 被丢，但重连条卸载会重触 ResizeObserver→`sshResize` 自愈，无用户可见后果）· `execCommandWithExitCode(id,cmd,timeout=30)` `:844-926`（经 `resolveSessionForExec:823-841` 对重连中会话最多等 `EXEC_RECONNECT_WAIT_MS`=10s；单字段 1MB 上限 `:28,885-889`；抛 `SESSION_NOT_FOUND`/`EXEC_TIMEOUT`）· `execCommand(id,cmd)` `:929-932`（`stdout||stderr||''`）· `getCwd(id)` `:935-938`（OSC 7，首次上报前为空）· `disconnect(id)` `:941-945`（= cancelReconnect + teardownSession，**不发 `ssh:close`**）· `disconnectAll()` `:948-969` · `reconnect(id,cols?,rows?)` `:989-997`（无条目抛 `SESSION_NOT_FOUND`）· `cancelReconnectsByConnection(connectionId)` `:1135-1139`（main.ts:1159 在 `ssh:deleteConnection` 上调）· `testConnection(config)` `:1161-1202`（**不进 `sessions`**；`done` 包在 `connect()` 外，`:1195` 之前同步完成会留一个 ≤17s 的悬空兜底 timer，因 `settled` 短路而无害）。

私有核心（调用方推理状态时需要的不变量）：`verifyHostKey` `:484-499`（每连接闭包 `:557`；TOFU **先存后**提示；不匹配 → emit + 返回 false）· `destroySession(id, client?)` `:523-537`（client 身份守卫 `:527`、`closeNotified` 去重、恰好一条 `ssh:close`）· `buildConnectConfig` `:540-597`（`readyTimeout??15000`、`keepaliveInterval??10000`、`keepaliveCountMax:10`、`compress:true`、`assertKeyFileSafe:576`）· `doConnect(id,config,withShell,cols?,rows?)` `:615-783`（上限 64 `:624-626`；重连条目登记 `:734-751`，原地更新规则见 `:731-733` 注释）· `beginReconnect:1000-1012`、`scheduleReconnectAttempt:1018-1036`、`runReconnectAttempt:1039-1105`、`isReconnectCurrent:1108-1110`、`cancelReconnect:1113-1132`、`teardownSession:1149-1158`。

id 空间：单一 `nextId` 计数器同时被 `ssh-`/`sftp-` 前缀消费（`:389,:601,:610`），**并且后端桥也用同一个**（`electron-bridge.ts:130 connectSftp`、`:139 disconnect`）⇒ 渲染层可见 id 会跳号，同进程内无碰撞。这条与 **S0-06**（桥无会话归属守卫）同源，跨切片引用时注意。

SFTP 面（1237-2089 归 S5，此处只登记名称与接线）：`requireSftp:1208-1212`、`openSftp:1215-1234`（晚到 `close` 有 wrapper 身份守卫）、`closeSftp:1237-1243`；handler 可见名 `sftpList/Mkdir/MkdirRecursive/Exists/Rename/Delete/Upload/Download/Cancel/ReadFile/WriteFile/ZipRemote/UnzipRemote`，全部接在 `main.ts:1266-1312`。

## 闸门

- `npx vitest run src/utils/qc-align.test.ts src/composables/bulkCommandDiff.test.ts` —— 只读引用，未整目录跑（并行会话持 SQLite 锁 + 共享库风险，见 `00-INDEX.md` 警示块）。
- `git diff a8e588c --stat -- <本片 9 个渲染层/主进程文件>` → 空 ⇒ 本片行号即锚点行号，可放心引用 `git show a8e588c:<path>` 复核。
- `grep -rni audit electron/ --include=*.ts` → 0 命中（S4-07 的可执行证据）。
- 其余各条以「打开原文」方式逐条自查，见每条 LOCATION 的行号；**未执行 pytest**（本片无后端主体，S4-07 涉及的路由证据取自 `routes/agent.py:1499-1515` 静态原文）。

## 已核对但未采纳 / 判定良性（登记以免后续重复怀疑）

- `ssh:connect` 载荷多余字段、`sshCompleteCommand.prevSegments` 必填/可选不一致、`ptyGetCwd → Promise<string>` 由 `main.ts:912` 的 `?? ''` 兜住 —— 三条均**良性**，见缝表。
- `resize()` 无会话时丢维度：自愈（见档案），不立案。
- `BulkCommandModal.vue:46,168-181` 的「{{doneCount}}/{{checkedCount}} 完成」：`results.length` 与 `liveChecked.length` 基数不同，tab 中途关闭可显示「3/2 完成」；进度百分比在 `:180` 已钳制 ⇒ 观感、需构造路径，不立案。
- `bulkCommandDiff.ts:28`（`resultsConsistent` 过 `exitCode !== null`）vs `:49`（`diffResults` 只过 `!error`）：超时主机（`exitCode:null`，`ssh-manager.ts:904-914` 无退出事件即 resolve）在 diff 里算不一致、在一致性谓词里被丢弃。两侧各自都说得通，**没有**得出错误的用户可见结论 ⇒ 不立案。
- `quickCommands.ts:110-126 reorder`（先摘源再算目标下标，正确）、`:44-57 load` 逐项净化（可靠）—— 无发现。
- `verifyHostKey` 在用户确认前先写 TOFU 指纹、`assertKeyFileSafe` 放行 `userData` 下全部路径：均为 `:471-483` 自述的设计，且不提供超出 `fs:readFile` 的读原语 ⇒ 已裁决族，不重议。
- `useTerminalContext.syncCwd` 卸载后写 `cwdBySession`：只在亚毫秒 IPC 往返内可达且被 `removeSession:324-325` 自愈 ⇒ 过于投机，不立案。
- `pty-manager.ts:117-129` 白名单未命中时回退 `defaultShell` 且不复查白名单：回退值来自 env 而非渲染层，落在 M3 威胁边界之外 ⇒ 登记不立案。
- 批量弹窗「执行中关闭只警告、在途 `sshExec` 不可取消、`results` 卸载后继续累积」（`:128-143`）：`sshExec` 通道本身无取消原语，属 S5-11（cancel 对拷贝 no-op）同一族根因，**归 S5/总表引用**，本片不重复计数。
