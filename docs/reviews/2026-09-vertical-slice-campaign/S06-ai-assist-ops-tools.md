# S6 · AI 命令行辅助 / explain / web 运维（含 ops_job 883 行）

锚点 `a8e588c`。范围：`routes/terminal.py:81`(predict) / `routes/chat.py:51`(upload，边界归 S9) / `harness/command_explain.py` / `tools/builtin/shell_exec.py` `web_tools.py` `ops_service.py` `ops_obs.py` `ops_job.py` / `main.ts:1320`(ssh:completeCommand) `:918`(pty:completeCommand)；渲染层 `SshAiPanel.vue` `CompletionPopup.vue` `stores/ssh-ai.ts` `useCompletion.ts` `useCompletionPopup.ts` `useTerminalContext.ts`。

**行号来源**：`useCompletion.ts`、`useCompletionPopup.ts`、`useTerminalContext.ts`、`ops_job.py`、`ops_obs.py`、`ops_common.py`、`web_tools.py`、`electron/ssh-manager.ts`、`electron/main.ts`、`node_modules/@xterm/xterm` 经 `git diff a8e588c --stat` 确认空差异 ⇒ 工作树行号即锚点。`routes/agent.py`、`database.py`、`contracts.py`、`llm_client.py`、`stirrups.py`、`client.ts`、`ssh-ai.ts` 属并行会话在写的脏文件 ⇒ 一律标 anchor（`git show a8e588c:<path>`）。

## 两条「核实存在、按勿重议登记」的前置结论

- `command_explain.py:396-404` 的缓存命中 `create_task` 延迟在位，且 `_explain_inflight.add()` 落在无 await 的同步段（`:424` 先于 `:25`）⇒ **无发现**。
- `ops_job` 本地分支**不使用** `shell=True`：`sandbox_exec.py:181` 是 `create_subprocess_exec`，argv 来自 `_split_cmd` ⇒ **无 shell 注入面**。
- anchor `ssh-ai.ts` 对 S6 面**零引用**（无 predict/completion/explain，grep 只命中 `:162` 的 `isTerminal`）；其工作树 delta 是预算项（`maxCostUsd/maxTotalTokens/breakerDisabled`），不碰补全链 ⇒ 本切片的渲染层结论不受该文件在写影响。

## 发现表

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S6-01 | a | **Critical** | ✅ | `readCurrentLine` 用**视口相对**行号去索引**绝对**行存储 ⇒ 补全读到的是游标上方 `ybase` 行之前的历史输出，并把候选**直接写进活动 shell** | `useCompletion.ts:168-176`（消费点 `useCompletionPopup.ts:59,70,133,149`） | 恒（任何一次输出滚动过视口之后） |
| S6-02 | c | Major | ✅ | `job_run` 在 await 之前预留 job_id、之后才登记 ⇒ 两个并发 run 撞同一 id 与同一远程目录（`>` 截断前一份 out.log、覆盖 run.sh/rc、丢 pid 句柄） | `ops_job.py:563` → `:583-587`（`_next_id:118-128`、`add:130-132`） | 交互式 run + 定时 run（S7）同时调 job_run |
| S6-03 | c | Major | ✅ | `job_kill` 用 `kill -0 <leader pid>` 验证终止而非进程组 ⇒ 自我 daemon 化的命令被报「已终止」而实际仍在跑，SIGKILL 连发都没发 | `ops_job.py:297-305` + `:418-431` + `:836-841` | `cmd="nginx"` / `redis-server --daemonize yes` / 任何以 `&` 收尾 |
| S6-04 | c | Major | ✅ | `out_offset` 按 **UTF-16/有损解码后的字符串长度**推进而非远程字节数 ⇒ 非 UTF-8 或跨包多字节字符的日志**永久丢字节**，且漂移被落盘 | `ops_job.py:410` ↔ 该文件自述不变量 `:377-384` ↔ `ssh-manager.ts:890-899` | 中文输出跨 TCP 包切分 / GBK 作业日志 |
| S6-05 | c | Major | ✅ | 复用 id 的新作业**继承上一份 `rc` 文件** ⇒ 刚启动的作业被立刻判「已完成」并拿到死作业的退出码，随后 `job_kill` 拒止 | `ops_job.py:270-283`（从不删 rc）+ `:118-128` + anchor `database.py:2786-2799` | 让作业结束或 kill 掉 → 重启应用 → 新作业首发即中 |
| S6-06 | b | Major | ✅ | `/terminal/predict` 没有任何可达调用者：AI 预测闸门要求「空前缀」，而 `parseInput` 在到达该状态前就返回 null ⇒ 那条 15s 后端 LLM 腿与「AI 预测」开关都是空转 | `useCompletion.ts:324` + `:230` ↔ `useCompletionPopup.ts:70-76` | 恒（正常用法下该特性完全不可达） |
| S6-07 | d | Major | ✅ | `network_probe` 的 `[本地监听]` 段落**永远填不上**：判据整串不含 `[exit_code=`，而 `run_headless` 恒追加该哨兵 | `ops_obs.py:238-246` ↔ `ops_common.py:143-144` ↔ `electron-bridge.ts:113`；对照同文件 `:161` 的**逐行**正确写法 | 每一次 `network_probe` 调用 |
| S6-08 | d | Major | ✅ | `web_fetch` 没有总时限，`meta.timeout` 无人执行：httpx 的标量 timeout 是**每次套接字操作**，慢滴流服务端可把一个 ≤10MB 响应拖成分钟级并钉住整个 run | `web_tools.py:395` + `:463-496` + `:359`（meta 无 timeout）↔ anchor `driver.py:539`（`saddle.execute` 外无 `wait_for`） | 模型给出的任意 URL（慢 CDN 即足够） |
| S6-09 | b | Minor | ✅ | predict 请求恒传 `cwd: ''`，而端点与它的 system prompt 都在消费该字段 | `useCompletion.ts:336-342` ↔ anchor `routes/terminal.py:48,101-102`、prompt 规则 6（`:71`） | 恒（prompt 规则形同虚设；`session_id` 亦无人读） |
| S6-10 | b | Minor | ✅ | predict 的 `platform` 由**应用 UA** 推导 ⇒ 远端 Linux 会话被声明成 "windows"，且忽略 `queryCompletion` 早已知道的 `side` | `useCompletion.ts:341` ↔ `:381,397,408` ↔ anchor `routes/terminal.py:50,104` | 潜伏 —— 藏在 S6-06 之后，一旦修好可达即发作 |
| S6-11 | b | Minor | ✅ | SSH 补全腿没有专属超时（30s 默认），而本地两条腿都在 5s kill 子进程 | `main.ts:1320-1330` → `ssh-manager.ts:929-932` → `:844-864`（`timeout=30`）↔ `main.ts:990-993`、`:1014` | 任何 rc 文件阻塞的远端会话（conda init、网络挂载 home） |
| S6-12 | b | Minor | ✅ | 两个 shell 生产者都在发 `cmd:` 标签，而唯一消费者把它整个丢弃 ⇒ CMD 的命令位补全恒 0 候选、PowerShell 的命令名匹配算完就扔 | `main.ts:929-930`、`:967-968` ↔ `useCompletion.ts:435,445-446`（`hiddenTags` 含 `func/alias/cmd`） | 本地 cmd.exe / PowerShell 的命令位 |
| S6-13 | b | Minor | ✅ | PowerShell 补全把用户 token 里**每个 `@`** 都改写：单引号 here-string 不做转义处理，`'@'→'@{'` 改的是正文 | `main.ts:953-954` ↔ `:947`（真正需要转义的 `'` 已处理） | 本地 PowerShell 里 Tab 一个含 `@` 的 token（`git send-email --to=a@b` → `a@{b`） |
| S6-14 | b | Minor | ✅ | `stripPrompt` 的 prompt 边界可被**用户自己正在输入的内容**移动 | `useCompletion.ts:95,207-217` ↔ `main.ts:281-296`（`complete -p` 那条 `arg:` 候选来源被跳过） | `echo a > /tmp/x` + Tab 这类常规重定向；csh/zsh 的 `# `/`% ` 同理 |
| S6-15 | d | Minor | ✅ | `stripPrompt` 的 OSC 6973 精确通道**双重不可达**：全仓无人发出该序列，且 xterm 把 OSC 消费进解析器状态、从不落进单元格文本 | `useCompletion.ts:92-93,198-205` ↔ `BufferLine.translateToString:524-538`；`grep -rn 6973 src electron backend scripts` 只命中这个消费者 | 恒（被文档化的机制静默缺席，边界全靠启发式回退） |
| S6-16 | a | Minor | ✅ | 三处远程命令拼接「今天安全」纯靠调用方约束，无任何断言：b64 裸嵌单引号内、pid 无 `int()` 强制、`log_dir` 未加引号 | `ops_common.py:207`、`ops_job.py:300-304`（对照启动路径的 `_parse_remote_pid:255-263`，恢复路径 `:207` 无校验）、`ops_job.py:279-282/291-293/321` | 当前不可达（所有值内部生成）—— 登记为**未断言的不变量** |
| S6-17 | c | Minor | ✅ | `exited` 对 Manager 是终态、对 DB 不是 ⇒ 僵尸行每次重启都被恢复，且永久占号 | `ops_job.py:153` ↔ anchor `database.py:2756`、`:2793-2795`；`grep DELETE FROM agent_jobs` anchor 零命中 | 任何一次作业转 `exited` 之后的重启 |
| S6-18 | c | Minor | ✅ | 恢复的 `start_ts` 用 `time.mktime` 解一个 **UTC** 串 ⇒ `job_list` 报出数小时幻影运行时长，并让淘汰顺序永远先踢恢复项 | `ops_job.py:194-198`（消费 `:523-524`、`:672`、淘汰 `:167`）↔ anchor `database.py:411` `DEFAULT (datetime('now'))` | 有活跃远程作业时的后端重启 |
| S6-19 | c | Minor | ✅ | 作业路径上的同步 sqlite 跑在事件循环线程里，违背本仓既有的 M-13/S11 约定 | `ops_job.py:183`、`:219/:236/:245`（均由 async `execute():562,656,749,822` 与 `_drain:493-499` 抵达）↔ anchor `database.py:77-88` 线程本地连接、`:67` `busy_timeout=5000` | 重启后首个 job_* 调用、每次状态/偏移迁移 |
| S6-20 | c | Minor | ✅ | S0-06 的加重：恢复的作业在**从未校验**的持久化 session id 上执行命令并收 SIGTERM/SIGKILL —— 且部分路径完全没有用户审批 | `ops_job.py:206,250-252,690-692,449-453`；`kill_remote_jobs_for_run` 由 anchor `routes/agent.py:1215-1216` 的 `asyncio.shield(...)` 自动触发 | 重启时有未完成远程作业 + 新终端会话复用 id 空间 |
| S6-21 | c | Minor | ✅ | 目标机重启后的 PID 复用，使 `job_kill`/`job_list` 变成「对陌生进程组动手」 | `ops_job.py:207`（pid 直取 DB，无 `int()` 也无存活证明）、`:297-305`、`:308-314` | 远程作业在册期间目标机重启/轮换之后 |
| S6-22 | c | Minor | ✅ | `job_run` 的入库是远程启动成功之后的 best-effort：DB 失败 ⇒ 目标机上留下**永远无法 kill 的孤儿守护进程** | `ops_job.py:583-588` + `:216-230`、`:233-238`（`except Exception: pass`，且无日志） | 恰在 job_run 时的 sqlite 写压力/磁盘满 |
| S6-23 | c | Minor | ✅ | `keep_full` 挡不住主进程侧的 1MB 头部保留截断，于是**截断提示语的字节数被计进 offset** | `ops_job.py:391,396-410` ↔ `ssh-manager.ts:884-893,908-913`（`TRUNCATED_NOTE`） | 两次 job_output 轮询之间产出 >1MB 的作业 |
| S6-24 | c | Minor | ✅ | `[stderr]` 段落被计进日志段 ⇒ offset 多推进那些**从未存在于 out.log** 的字节 | `ops_job.py:396-410` ↔ `ops_common.py:141-144`（stderr 拼在哨兵之前）；`:382-383` 的「有 stderr 即 exit≠0」假设此处不成立 | 窄（存在但不可读的 rc 文件，如共享 /tmp 里他人所有的陈旧作业目录） |
| S6-25 | c | Minor | ✅ | 对账走的是**位置对齐协议**却没开 `keep_full` ⇒ 中间行被中文标记替换后，退出码错位归属到别的作业 | `ops_job.py:692,697-700,707-719` ↔ `truncate_output`（阈值 4000，`truncation_limits.py:8-12`）↔ `ops_common.py:109-113` 自己写的规则 | 单会话约 300+ 运行中远程作业时（低概率，但是协议类错误） |
| S6-26 | b | Minor | ✅ | `_split_cmd` 在 Windows 宿主上做 POSIX 词法，反斜杠路径被吃掉 —— 而 docstring 承诺相反 | `ops_job.py:464-474`（声明 `:467-468`）↔ `tests/test_ops_file_job_img.py:430` 已承认该行为 | 仅 `CKUN_AGENT_ALLOW_UNSANDBOXED_LOCAL=1` 时；否则 `sandbox_exec.py:168-170` 提前 fail-closed |
| S6-27 | a | Minor | ⚠️ | 选区按钮定位的 x/y **同时**差一：`IBufferCellPosition.x/.y` 都文档化为 1-based | `useTerminalContext.ts:75,77` ↔ `@xterm/xterm/typings/xterm.d.ts:1488-1498` ↔ `BufferApiView.ts:24`（`viewportY` 0-based） | 每次「问 AI」按钮定位 |
| S6-28 | d | Minor | ✅ | 三条搜索腿没有响应体上限，Tavily 结果也不按 `max_results` 截断，而 fetch 路径是专门加了上限的 | `web_tools.py:164`、`:197`、`:266` ↔ 刻意的 `:21,463-496`；`_format_results:321-328` 无总输出上限 | 固定站点 ⇒ 多为「大页面」而非攻击；数 MB HTML 上的 `re.finditer(DOTALL)` 还是循环线程上的 CPU 活 |
| S6-29 | d | Tradeoff | ✅ | `web_fetch` 只解析 A 记录 ⇒ IPv6-only 目标一律报「DNS 解析失败」，双栈目标恒钉 A 记录 | `web_tools.py:523` → `:529` → `:498-509`；设计意图在 `:392-394` 有文档 | v6-only 主机名（在该工具的目标集里少见） |

## 详情

### S6-01 [dim a][Critical] 两套行号坐标系在同一行代码里混用
- LOCATION：`src/composables/useCompletion.ts:168-176`
  ```ts
  const buffer = terminal.buffer.active
  const cursorY = buffer.cursorY
  const cursorX = buffer.cursorX
  const line = buffer.getLine(cursorY)     // ← 缺 baseY
  ```
- 我把三个事实逐字核到底层库（不是采信引用）：
  1. `node_modules/@xterm/xterm/typings/xterm.d.ts:1509-1514` —— `cursorY`：「This ranges between `0` (when the cursor is at baseY) and `Terminal.rows - 1`」⇒ **视口相对**。实现：`src/common/public/BufferApiView.ts:21` `get cursorY(){ return this._buffer.y }`。
  2. `BufferApiView.ts:27-32` —— `getLine(y)` 就是 `this._buffer.lines.get(y)`，而 `lines` 是包含 scrollback 的**绝对**行存储（`length = ybase + rows`）。
  3. xterm 自己的换算写法：`src/common/buffer/Buffer.ts:96` 与 `:375` 都是 `const absoluteY = this.ybase + this.y;` ⇒ 游标所在绝对行 = `baseY + cursorY`。公共 API 恰好把这两个数都暴露出来（`typings:1527-1531` 的 `baseY`、`:1525` 的 `viewportY`）。
- CLAIM：终端一旦滚动过（`ybase > 0`），`getLine(cursorY)` 返回的是**游标上方 `ybase` 行**的那条历史输出。于是：`stripPrompt`（`:197-223`）从一条任意旧行里刮出「input」，`parseInput`（`:230+`）据它产出 prefix，而 **`applyCompletion`（`useCompletionPopup.ts:126-143`）用同一个错误行重新计算 prefix**，把 `buildCompletionText()` 的差量 —— 候选不匹配时就是**整条候选 + 空格** —— 经 `deps.write`（`RemoteTerminal.vue:155`）写进活动 shell。`triggerCompletion:89-91` 的**唯一候选自动补全路径不经任何确认**。再叠加 `collectCommand`（`:146-154`，Enter 时在 `RemoteTerminal.vue:396` / `LocalTerminal.vue:219` 调用）把那条垃圾行记进 `sshStore.recordCommand`，而它正是 `queryHistoryCandidates`、`queryChainCandidates` 与 `/terminal/predict` 的 `recent_commands` 的输入源 ⇒ 缺陷自我强化。
- 同仓反证（说明这不是口径之争）：`useCompletionPopup.ts:112-113` 把同一个 `cursorY` 当**视口相对**用于像素定位（正确），而 `useTerminalContext.ts:72-73` 的注释明确写着反方向规则：「start.y 是 buffer 绝对行号，必须减去视口滚动偏移 `viewportY`」。⇒ 同一个库的同一个值在本仓被按两种坐标系用，其中一种是错的。
- 可达性：正常交互使用即达 —— 任何一次输出滚动过视口（第一次 `ls`/`df` 就够）之后，`ybase>0` 恒成立。
- **为什么判 Critical**：不是观感问题 —— 它会在用户没确认的情况下改写正在输入的命令行（下一步 Enter 就是执行），并把错误内容写进喂给预测/历史的功能面。修法面很窄（改成 `buffer.baseY + buffer.cursorY`，或按 `useTerminalContext` 那条注释口径统一到同一坐标系），但**裁决前请先确认 `getLine(baseY+cursorY)` 在选区/回滚视图下的期望语义**，因为该函数也被 `collectCommand` 在 Enter 时刻使用，此刻 `ydisp==ybase`，两种写法在该调用点等价。

### S6-02 [dim c][Major] 号在 await 前预留、条目在 await 后才登记
- LOCATION：`ops_job.py:559-587` —— `mgr._ensure_restored()` → `job_id = mgr._next_id()`（`:563`）→ 构造 `entry` → `ok, pid, err = await _remote_launch(context, job_id, cmd)`（`:583`，最长 20s）→ `mgr.add(entry)`（`:587`）。`_next_id:118-128` 只把**已在 `self._jobs` 里**的号视为已占用；`add:130-132` 是唯一登记点。
- CLAIM：`JobManager` 是进程单例（`:101-116`），所以第二个并发 `job_run` 看到同一个号是空闲的，起进同一个 `/tmp/ckun_agent_jobs/<id>`；启动模板里的 `> {log}`（`:279-282`）**截断第一份 out.log**，`run.sh`/`rc` 被覆写；随后 `mgr.add` 直接替换掉第一个 `JobEntry` ⇒ 前一个作业的 pid 句柄与 kill 通道一起丢失。本地分支同型（`:614-637` 先 await `run_sandboxed` 再 add），孤儿子进程的 reader task 与输出缓冲被丢弃。
- 可达性：交互式 run + `schedule_engine` 的无头 run（见 S7）同时调 `job_run`，或两个面板的两个 run。单次 run 内部的连续工具调用是安全的（driver 按序迭代，anchor `driver.py:539`）。`test_job_id_reuse_round10.py:30-43` 只钉住了「查内存」这一行为，没覆盖并发窗口。
- 注：`:119-123` 的注释显示当年已经为**跨重启**撞号修过一次（`_ensure_restored` 前置），这次的洞是**同进程并发**，是同一条不变量的另一半没守住 —— 与 S4-05 的 `releaseConnectLock`、S7 的 `_executing.add` 位置属同一族写法问题。

### S6-03 [dim c][Major] 发信号按进程组、验生死按组长
- LOCATION：`ops_job.py:297-305`
  ```ts
  `kill -TERM -- -{pid} 2>/dev/null; sleep 3; ` +
  `if kill -0 {pid} 2>/dev/null; then ` +
  `  kill -KILL -- -{pid} 2>/dev/null; sleep 1; ` +
  `  kill -0 {pid} 2>/dev/null && echo CKUNJOB_STILL || echo CKUNJOB_DEAD; ` +
  `else echo CKUNJOB_DEAD; fi`
  ```
  消费点 `:418-431`（`_remote_kill`）与 `:836-841`。
- CLAIM：两处**验证**用的都是 `setsid` 包装进程的 PID。若被包命令退出或自我 daemon 化，组长已经没了而组成员还活着 ⇒ `kill -0 pid` 失败 ⇒ `echo CKUNJOB_DEAD` ⇒ `_remote_kill` 返回 "DEAD" ⇒ `update_status("killed")` ⇒ `ToolResult(success=True, "已终止远程 job…（进程组 … 已退出）")`。**存活的组从没收到过 SIGKILL**。当年 R2 那个 "STILL" 诚实性修复只覆盖了「组长还活着」那一支。文件里正确写法就在同一处信号语句上（`-- -{pid}`），验证却没跟上。
- 可达性：`job_run target=remote cmd="nginx"`、`"redis-server --daemonize yes"`、任何以 `&` 收尾的命令 —— 全在该工具自己宣传的「nohup 语义，关窗口继续跑」（`:532-534`）能力面内。旁证可从诚实的 `rc` 症状读到：`run.sh` 在包装脚本退出时就写 rc（`job_launch_cmd:276`），所以 `job_output` 会在启动后几秒就把 daemon 报成「已结束、exit_code=0」。

### S6-04 [dim c][Major] 字节守恒不变量栽在解码环节
- LOCATION：`ops_job.py:410` `entry.out_offset += len(tail.encode("utf-8"))`；不变量由该文件自己在 `:377-384` 写明：「offset 每次推进的量必须等于 **out.log** 自上次 offset 以来新增的字节数……多推 → 永久跳丢日志」。
- CLAIM：`tail` 是 JS 字符串，按 TCP 分片用 `data.toString('utf-8')` 拼出来（`electron/ssh-manager.ts:890-899`，**全仓 `grep StringDecoder electron/` 零命中** ⇒ 无跨分片状态机）。因此 (a) 任何非 UTF-8 字节序列、(b) 任何被切在两个 `data` 事件之间的多字节字符，都会变成 U+FFFD，而 U+FFFD 在 Python 侧重新编码是 **3 字节**。于是对 GBK/二进制污染的作业日志，或任何中文作业输出恰好跨包时，offset 按 out.log 真实字节数的约 3 倍推进，`tail -c +{offset+1}`（`:293`）就把对应字节**永久跳过**；`:414` 的 `_persist_offset` 还把这个漂移落盘、跨重启存活。
- 佐证：`ops_common.exec_remote:137-146` 把字符串原样透传；现有测试只对 ASCII 钉字节精确性（`test_ops_job_remote.py:239-274`）。
- 同一函数上的另外三个兄弟缺陷（同一根因家族，均已在表内单列）：S6-23（1MB 头部保留截断的**提示语**被计进 offset，`ssh-manager.ts:908-913`）、S6-24（`[stderr]` 段被计进日志段，`ops_common.py:141-144` 把它拼在哨兵之前）、S6-25（对账的位置协议没开 `keep_full`）。

### S6-05 [dim c][Major] 回收的 id 会带着上一任的退出码复活
- LOCATION：`ops_job.py:270-283`（`job_launch_cmd` 从不 `rm -f {rc}`，`out.log` 靠 `>` 截断而 `rc` 不会）+ `:118-128`（`_next_id` 只看内存）+ `:190-212`（恢复）+ anchor `database.py:2786-2799`（`get_running_agent_jobs` 的排除表把已终结行滤掉）。
- CLAIM：终态行（finished/failed/killed）永远不会被恢复 ⇒ 它们的号在后端重启后就是**空闲号**，而远端 `/tmp/ckun_agent_jobs/<id>/rc` 还躺在那里。新作业首发落到该号 ⇒ 第一次 `job_output` 就解析出 `CKUNJOB_RC=<陈旧码>`（`:399-403`），`JobOutputTool:770-771` 立刻把新条目改成 finished/failed 并挂上死作业的退出码；随后 `job_kill` 在 `:826-830` 以「已不在运行」拒止，**而进程还在跑**。
- 持久面：anchor `database.py:2719-2744` 的 upsert 不重置 `exit_code/finished_at/out_offset`；`:158-169` 的内存淘汰不碰 DB；`grep DELETE FROM agent_jobs` 在 anchor `database.py` **零命中** ⇒ 行永不删除。883 行的文件里没有任何 rc/目录清理路径。
- 与 round-10 那次修复的关系：这条**加重**而不推翻它（那次修的是「重启后 _seq 归零撞内存里在册的号」）。

### S6-06 [dim b][Major] 一整条后端功能没有可达入口
- LOCATION：闸门 `useCompletion.ts:324` `if (!input.isCommand || input.prefix) return []`（AI 腿要求「命令段 + 空前缀」）；唯一调用者 `:494` 又嵌在 `if (sessionId && input.isCommand && !input.prefix)`（`:487`）里；阻断在 `parseInput` 头部 `:230` `if (!line) return null`；上游 `useCompletionPopup.ts:70-76` 拿到 null 即 `hideCompletion()` 并 return。
- CLAIM：`isCommand && !prefix` 要求「剥离后的输入非空，但游标前的 token 为空」。而 `stripPrompt` 的两个分支（`:213-216`、`:219-222`）都把前导空白切掉，所以该状态只在 `cursorIdx==0` 时可达（即光标已在一行既有文本的第 0 列）。设计意图的触发形态 —— 空提示符上按 Tab —— 产生 `input === ""` ⇒ `parseInput` 返回 null ⇒ 直接收起。结论：`queryAIPredictions`、`queryChainCandidates`（`:291-311`，同闸门形态）与整条 15s 后端 LLM 腿（anchor `routes/terminal.py:81`）**从不执行**；「外观 → 终端 → AI 预测」开关（`AppearancePanel.vue:148`，默认 false，`settings.ts:248`）拨的是空操作。唯一可达的那次还会把候选插到第 0 列（行中间）。
- 证据：`grep -rn "predictTerminalCommand" src/` 只有 `useCompletion.ts:336` 一个调用者；xterm 把用户敲的空格存成真实码点（`InputHandler.ts:631`、`BufferLine.ts:219`、`getTrimmedLength:461-468`）⇒ 尾部空格并没有「被吃掉」，空的原因是 `!line` 这一跳，不是 trim。

### S6-07 [dim d][Major] 段落级判据撞上恒定追加的哨兵
- LOCATION：`ops_obs.py:238-241`
  ```python
  _, ss_out = await run_headless(context, netprobe_listen_cmd(str(port)), timeout=15)
  if ss_out.strip() and EXIT_CODE_PREFIX not in ss_out:
      listen = ss_out.strip()
  ```
  而 `ops_common.py:143-144` 无条件 `if exit_code is not None: full += f"\n{EXIT_CODE_PREFIX}{exit_code}]"`，`electron-bridge.ts:113` **总是**带上 `exit_code`。
- CLAIM：`EXIT_CODE_PREFIX not in ss_out` 对**每一次成功的 ss/netstat** 都是假 ⇒ `listen` 恒为 ""，工具永远输出「[本地监听 h:p]（无监听信息）」；`:242-245` 的 netstat 回退因同一原因也不可达。该条件只在「执行通道关闭却没拿到退出码」（`exitCode === null`）这个失败形态下才成立。**同文件已经知道正确写法**：`:160-162` 是逐行 `if EXIT_CODE_PREFIX in line: continue` ⇒ 文件内部口径漂移。
- 与已裁决项的关系：91 清单 item 15 只裁过 `ops_obs.log_read:319-324` 那条死分支，本条是**新类**，不构成重议。
- 为什么判 Major 而不是 Minor：这是一个成文的运维入口，它对「端口是否在听」这个核心问题**永远给出否定答案**，而模型与用户都会把它读成事实。

### S6-08 [dim d][Major] 超时声明存在，执行者不存在
- LOCATION：`web_tools.py:395` `async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client`，配 `:463-496` 的 `aiter_bytes(65536)` 流式循环；`:359` `meta = ToolMeta(risk_level="safe", reversible=True, cacheable=True, idempotent=True)`（**没有** `timeout` 形参）；执行侧 anchor `driver.py:539` `result, undo = await harness.saddle.execute(tc, ctx, ...)`。
- CLAIM：httpx 的标量 timeout 是**每次套接字操作**的超时，不是整请求 deadline ⇒ 慢滴流服务端每 <15s 吐一个 64KB 块，就能在 ≤10MB 上限内把这次调用撑住几分钟；`:404-423` 的重定向循环最多再乘 6。外层没有任何 `wait_for`/`asyncio.timeout`（`grep -n "asyncio.wait_for\|asyncio.timeout" driver.py saddle.py` 在 driver 里只有 `:706` 那一处，管的是 PendingDeliveries 等待），`meta.timeout` 的读者除工具自己之外只有 anchor `routes/agent.py:1321` 的那份**建议性清单** ⇒ `tools/__init__.py:32` 的默认 30.0 是装饰。结果：**模型选一个 URL 就能钉住整个 run**。
- 可达性：任何模型传给 `web_fetch` 的 URL（敌意页面只需「慢」，一个慢 CDN 即足够）。用户能中止，但期间 run 被占住且工具结果永远不落地。SSRF 那一腿维持既有裁决，本条**只报时间维度**。

### S6-27 [dim a][Minor] ⚠️ CONTRADICTION — needs re-adjudication
- 与 91 清单 item 20② 冲突（当年以「需 DOM mount 验证」延后 `positionAskAiBtn` 选择 x 疑似差一列）。新证据不需要 DOM：`node_modules/@xterm/xterm/typings/xterm.d.ts:1485-1498` 把 `IBufferCellPosition` 的 **x 与 y 都注明 1-based**；`useTerminalContext.ts:75` 的 `relY = (start.y - viewportY) * cellHeight` 中 `viewportY` 是 0-based（`BufferApiView.ts:24` ⇒ `_buffer.ydisp`），`:77` 的 `x = start.x * cellWidth` 未减 1 ⇒ **两根轴同因各差一格**（y 低一行、x 右一列），`:79-84` 的 clamp 只把它部分掩盖掉。
- 按战役口径我不自行翻案：并列旧理由（缺证据）与新证据（xterm 自带 .d.ts 声明），标 `needs re-adjudication`。y 是新提出的一根轴，与当年被延后的 x 是不同维度，故一并列出。

## 全部远程/本地命令构造点清单（S6 的交付物，逐行核）

| 位置 | 被插值内容 | 引号处理 | 结论 |
|---|---|---|---|
| `ops_common.py:189` | `path` | `shlex.quote` | safe |
| `ops_common.py:195`（`rm -f`） | `path` | `shlex.quote` | safe |
| `ops_common.py:207`（`printf '%s' '…' \| base64 -d >`） | `b64`、`path` | path 加引号；**b64 裸嵌单引号内、无 escape** | 仅靠调用方契约安全（`base64_write_cmd:200`、anchor `saddle.py:206`），**无断言**（S6-16） |
| `ops_common.py:225`（分类视图） | 常量 payload、`path` | `shlex.quote` | safe；刻意不带 payload（键稳定性），`:210-223` 有文档 |
| `ops_common.py:142-144` | 哨兵文本 | 非命令 | n/a |
| `ops_job.py:276` | `{cmd}` 落进 run.sh 正文 | 无 —— 设计如此（远端执行），`cmd` 走 `_COMMAND_EXEC_TOOLS` 分类；Sandbox 缺口已记为 S0-01 | by design |
| `ops_job.py:279-282` | `d`、`script`、`log`、`rc`、`sb64` | **全无引号** | 构造性安全（`_JOBS_ROOT`+管理器生成的纯数字 id；`sb64` 是 base64 字母表） |
| `ops_job.py:291-293` | `log_dir`、`offset+1` | 无引号；offset 是 `int` | 构造性安全，但 `log_dir` 在恢复路径来自 DB（`:208`）**未复校** |
| `ops_job.py:300-304` | `pid` | 无；仅 `int` 注解 | 启动路径有 `_parse_remote_pid:255-263` 的 isdigit 校验，**恢复路径（`:207`）没有**（S6-16/S6-21） |
| `ops_job.py:312` | `pids` 裸 join 进 `for p in …` | 无 | 同上（每个元素在启动时是纯数字） |
| `ops_job.py:321` | `log_dirs` 裸 join 进 `for J in …` | 无 ⇒ 一旦目录名含空格/`*` 即词分割 + glob | 仅构造性安全 |
| `ops_service.py:64` | `action`、`name` | `action` 走 `_ACTIONS` 枚举（`:12`，`:96` 复校）；`name` 既过 `_NAME_RE`（`:17`）**又** `shlex.quote` | safe —— 本切片最强写法 |
| `ops_obs.py:28` | `top` | `clamp_int` 整数经 `%d` | safe |
| `ops_obs.py:49-56` | 无 | 字面量（远端白名单 `:41-47` 的无参契约有文档） | safe |
| `ops_obs.py:62` | `query` | `shlex.quote` | 注入安全；**无类型/控制字符防护** ⇒ 非 str 实参在 `quote()` 内抛 `TypeError` |
| `ops_obs.py:69-71` | `host`、`port` | 作为 `$1/$2` 位置参数 `shlex.quote`；脚本体单引号字面量 | safe（该模板里 `timeout` 实参未被使用） |
| `ops_obs.py:77` | `timeout`、`host`、`port` | 三处 `shlex.quote`，调用方 `:214-215` 已整数钳制 | safe |
| `ops_obs.py:83,89` | `port` | `shlex.quote` 进 `grep :{pq}` | safe；过度匹配（`:80` 也会命中 `:8080`）—— 仅展示层 |
| `ops_obs.py:94` | `lines`、`path` | `:300` 整数钳制 + `shlex.quote` | safe |
| `ops_obs.py:99` | `pattern`、`path`、`lines` | 两处 `shlex.quote` + int | safe；**`execute` 传原始 `pattern`（`:303`）而 `bound_command` 用 `str(pattern)`（`:286`）** ⇒ 非 str 崩溃，且两处授权视图不一致（S2 的 `bound_command` 口径问题在本切片的对应物） |
| `ops_job.py:602-621`（本地） | `_split_cmd(cmd)` 的 argv | 经 `run_sandboxed` 走 `create_subprocess_exec`（`sandbox_exec.py:181`），**全程无 shell** | shell 注入安全；POSIX 词法会切坏 Windows 路径（S6-26） |
| `main.ts:1326-1329`（`ssh:completeCommand`） | 用户 `prefix`/`cmd`/`prevSegments`、OSC-7 `cwd` | 逐值 `shellQuote`（`:241-243`）+ `shellQuoteArgs`，整段再过 `escapeBashScript`（`:251-253`）包进外层 `bash -lic '…'` | safe —— 两层引号正确复合（`'`→`'\''`），`$(…)`/`!` 都留在单引号内 |
| `main.ts:951-971`（`pty:completeCommand`，PS） | `prefix`、`prevSegments` | 单引号 here-string（字面量）+ `'`→`''`；经 `-Command` argv（不经 cmd.exe） | 不可注入；但 `@`→`@{` 改写破坏数据（S6-13） |
| `main.ts:1013-1014`（本地 bash） | 同一脚本体 | 同 `:1326` 去掉外层包装；`execFile('bash', ['-lic', body])` | safe（argv，无 shell 字符串） |
| `ssh-manager.ts:1992,2053`（相邻但非 S6 所有） | 生成的 `tmpScript` 路径 | `shellQuoteRemote`（`:2066`） | safe |

**未发现任何可达注入**。S6-16 那三处是「靠构造安全但没有断言护住」的未保护不变量，`shlex.quote`/`int()` 会让它们自证 —— 本仓已有该写法（`ops_service.py:17,64`、`ops_obs.py:62,94-99`）。

## 未覆盖声明

- **未执行** pytest / ruff / node（auto 模式还拦掉了两次只读探针：一次 `python -c` 的 shlex/sqlite 验证、一次对 `scripts/deploy/agent-exec.py` 的 grep）。`shlex.split` 的反斜杠行为与 `datetime('now')` = UTC 是按文档语义 + 本仓自己的测试注释（`test_ops_file_job_img.py:430`）断言的，不是跑出来的。
- **远端白名单跨端契约**：`scripts/deploy/agent-exec.py` 的正则 vs 新的 `job_*`/`service_cmd` 模板（`test_remote_whitelist_contract.py`）未读 —— 读清单划在片外且权限拦下。若某个 job 模板漂移，白名单会**静默拒绝**它。
- **目标机上 `setsid` 的 fork 语义**：`$!` 是否恒为新 PGID 取决于 sshd-exec shell 的进程组状态，静态不可证。上面的 `job_kill` 缺陷**不依赖**这一点；而 `kill -- -<pid>` 打错目标那一变体留为开放问题。
- **httpx `Host` 头覆盖优先级** vs URL 派生 host（`web_tools._get_with_limit:385-399`）需要已安装 httpx 源码（工作区外），未判。
- **渲染/UI 真实性**：弹窗像素数学（`positionPopup`）、CSS、`SshAiPanel.vue` 的 prefill watch —— 读过但未判（无运行时/DOM 证据；91 清单对姊妹案例同样是延后口径）。
- `useCommandDelivery.ts` 是 26 行的 focus-only 桩，有活调用者（`SshView.vue:237`、`QuickCommandBar.vue:116`）；`SshView.vue:236` 的过期注释还写着已删除的 `ssh:deliver-command` 事件 —— 登记不立案。
- anchor 文件只在 S6 依赖处判读：anchor `routes/agent.py`（`:34` 的 `_get_model_config` 导入，anchor `:1215-1216` 的取消清理）、anchor `database.py`（agent_jobs）、anchor `netconfig.py`（已核 `TIMEOUT_MIN 0` 的 delta 就是文档化的「不覆盖」哨兵，见 `http_timeout:180-182` ⇒ 对 web 工具无影响）、anchor `client.ts`/`ssh-ai.ts`（predict 契约未变；预算 delta 与 S6 无关）。`contracts.py`/`llm_client.py`/`stirrups.py` 的 diff 未审。
