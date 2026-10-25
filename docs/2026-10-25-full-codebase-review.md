# 2026-10-25 全仓代码审查报告

审查方式:四个专项深审(Electron 主进程 / 渲染层状态与 API / 渲染层组件 / Python 后端)+ shared·gateway·scripts·文档一致性巡检,所有发现均给出文件:行号与代码证据,重要发现经人工复核。

## 结论

- **无 [CRITICAL]**。SQL 全参数化、XSS 渲染链三层设防、IPC 面统一 sender 校验、审批链 fail-closed、TOFU/钉扎健壮、网关拒绝清单无绕过。
- **基线全绿**:typecheck ✓、vitest 1368 ✓、pytest 3155 ✓。
- 本次落地 3 项修复(见下),其余发现按模块登记,均不阻塞日常使用。

## 本次已落地的修复

| # | 严重度 | 内容 | 配套测试 |
|---|---|---|---|
| 1 | MAJOR | `backend/agent/harness/safety.py` 凭据文件判定补全:`.aws/credentials`、`.kube/config`、`.netrc`、`.git-credentials`、`.docker/config.json`、`.npmrc`、`.pypirc`、gcloud 凭据库此前 `cat` 免审直读;POSIX + Windows 两条正则严格对齐 | `backend/tests/test_safety_credential_file_families.py`(误拦锁:`.aws/config` 不收、兄弟名字不牵连、备份同罪) |
| 2 | MAJOR | `electron/ssh-manager.ts` `dropAllTransferConns`:定时器到点时若有在途租约,早退分支残留的 cleanupTimer 引用会让归还路径永不重排空闲回收 → 整批连接(含 keepalive)常驻到 disconnect。修法:无论是否早退先清空全部引用 | `electron/ssh-manager-transfer-idle-reclaim.test.ts` |
| 3 | MINOR | `CONTEXT.md` "ADR 0003" 悬空引用(仓库无 ADR 目录)→ 改指 `electron/backend-manager.ts` 的 `killOrphanBackends` 注释与 git history | — |

---

## 待处理发现

### Electron 主进程

- **[MAJOR·待确认] gh-download 同 URL 并发下载共用同一 `.ckpart`**(`electron/gh-download.ts:207-212`):两个同 URL 任务以追加模式交错写同一文件,最终 rename 成"完成"文件即损坏。若渲染层已按 URL 排队则降级为防御性建议;修法:按 urlKey 维护在途表,命中即拒绝或复用。
- **[MINOR] `fs:resolveProgram` 是路径存在性预言机**(`electron/main.ts:331-343`):`name` 未做段校验,渲染层可传 `..\..\.ssh\id_rsa` 探测任意路径存在性。修法:只允许裸程序名。
- **[MINOR] `pty:completeCommand` async executor 反模式**(`electron/main.ts:993`):畸形入参(如 `prevSegments` 非数组)抛 TypeError 会使 invoke 永不 settle。修法:普通 async 函数 + try/catch 兜底 + 入参类型守卫。
- **[MINOR] `execCommandWithExitCode` 超时 channel 泄漏**(`electron/ssh-manager.ts:1178-1186`):timer 到点时若 exec 回调未回,`streamRef` 仍为 null,随后创建的 stream 无人关闭。修法:回调里检查 settled 立即 close。
- **[MINOR] `~/.ssh/config` 的 ssh443 apply/remove 不在串行队列**(`electron/github-accel.ts:812-841`):连续调用存在丢更新窗口。
- **[MINOR] `gitrepos:*` 防线表述弱于实际**(`electron/git-repos.ts:100-112`):`assertRepoPath` 放行任意含 `.git` 的目录,而 `pullOne` 会执行该仓库 `.git/config` 里的 credential helper 与 post-merge hook。按威胁模型取舍:叠加敏感目录判定,或收敛注释表述。
- **[MINOR] `electron-bridge.log` 只增不滚**(`electron/electron-bridge.ts:12,19-23`)。
- **[MINOR] zipLocal/unzipLocal 用裸 `'powershell'`**(`electron/main.ts:916,935`),未复用 `utils/powershell-path.ts`(注释明言 PATH 被裁剪环境会 ENOENT)。
- **[MINOR] `webPreferences.sandbox: false`**(`electron/main.ts:150-155`;待确认 webUtils 在沙箱 preload 的可用性)。
- **[MINOR] 同 transferId 并发传输覆盖旧条目**(`electron/ssh-manager.ts:1938`;与 delete 侧 C10 守卫不对称)。
- **[MINOR] gh-download 无并发上限**(`electron/gh-download.ts:147-168`;对比 PTY 32 / SSH 64 / 隧道 256 的既有纪律)。
- NIT:requireSftp 区段缩进残留;`MAX_EXEC_OUTPUT_BYTES` 名为字节实为字符数;三份 shell 单引号转义(`main.ts` / `ssh-manager.ts` / `git-repos-core.ts`)可下沉 shared;网关错误中文正则兜底会随文案漂移;`zip -r` sources 建议加 `--`;PS here-string 把补全输入里的 `@` 静默改写;`backend-manager.stop()` 承担双语义。

### 渲染层状态与 API

- **[MAJOR] SSE 无停滞看门狗**(`src/api/sse.ts:284-321`):后端每 15s 发 `: heartbeat` 注释行,前端读取循环对 `data: ` 之外的行静默忽略,无"最后活动时间"判定。黑洞连接(休眠唤醒、NAT 静默丢流)会让 run 永远 `running`:审批卡挂着、计时无限涨。修法:任何行(含 `:` 注释)刷新 `lastActivityAt`,超阈值(建议 ~90s = 6× 心跳)主动 `reader.cancel()` 走 `onError('连接停滞')`。
- **[MAJOR] 终端输出捕获缓冲无上限**(`src/composables/useAgentDeliver.ts:114-129,449-452`):60s 捕获窗口内 `cap.output` 无界累加(一条 `cat 巨型日志` 即可累积几十 MB)并全量回传进轨迹。修法:设 256KB 上限,超限截断并追加标记。
- **[MINOR] 流末尾残留 buffer 未解析**(`src/api/sse.ts:291,323-329`):代理截掉最后一个换行时,完整收到的最后一个事件(甚至 [DONE])被丢并误报"响应中断"。
- **[MINOR] agent store 迟到事件守卫不对称**(`src/stores/agent.ts:317-321,818-835,908-941`):onError 有守卫,`error`/`done` 分支无;建议 `_handleEvent` 入口统一加 running 校验。
- **[MINOR] error 终态不 flush 思考块擦洗器**(`src/stores/agent.ts:908-915` 仅 done 分支 flush)。
- **[MINOR] 补全前缀计算与 parseInput 不同源**(`src/composables/useCompletionPopup.ts:135-137`):Tab/多空格输入时补全整串重发产生重复字符;复用 `parseInput().prefix` 即可。
- **[MINOR] server-monitor 会话关闭不清 retained 快照、重试定时器空转**(`src/stores/server-monitor.ts:104-123,249-255`)。
- **[MINOR] 启动合并只拉前 50 条会话**(`src/composables/useChatPersistence.ts:119` 硬编码一页):换设备后第 51 条起永不进本地。
- **[MINOR] 交付回传失败无重试、无用户侧恢复入口**(`src/composables/useAgentDeliver.ts:207-225`)。
- NIT:`toolCallByCallId.delete(tc.callId ?? '')` 删空键;`runRemoteCopy` 注释自指错误;信封正则双份(`server-monitor-core.ts` 与 `api/base.ts`)可下沉 shared;`Skill` 同名异形接口两处;`registerConfirmDialog` 不返回注销函数;`toast.items` 无上限。

### 渲染层组件

- **[MAJOR] "新开会话固定 sleep 500ms 再写命令"两处独立复制**(`src/components/ssh/DockerView.vue:833`、`src/components/ssh/git/GitReposPanel.vue:753`):慢主机(MOTD/profile >500ms)时命令与登录横幅交错、静默失败。修法:确认式握手(注入一次性标记再写真命令),至少抽共享常量 + 超时提示。
- **[MAJOR·待确认] MarkdownStream 内容清空残留旧 HTML**(`src/components/chat/MarkdownStream.vue:83-86`):`if (v === '') return` 跳过清空;当前调用方多以 `msg.id` 为 key 重挂载,线上未必可见,但属共享组件潜伏缺口。修法:清空时同步 `renderedHtml.value = ''`。
- **[MAJOR] 聊天消息流无虚拟化 + 逐 token 滚动**(`src/components/home/HomeWorkspace.vue:169-176`):`agentStore.content` 每秒触发数十次 watch → `nextTick` + 读 `scrollHeight` 强制 layout;长会话成本线性涨。修法:滚动 rAF 合帧 / 仅贴底时跟随;超长会话折叠渲染。
- **[MINOR] 三份手写右键菜单**(`SshView.vue:207-238`、`SftpView.vue:109-202`、`RemoteManager.vue:500-535`):定位钳制逻辑已漂移(SftpView 用了 `utils/menu.ts`,另两处没有);建议抽 `ContextMenu.vue` 收口。
- **[MINOR] 其余**:GitAccelPanel 对 store 数组 deep watch(`:473-479`);Ctrl+Shift+F 捕获不看焦点落点(`useXtermInstance.ts:154-160`);tab 右键 action 裸 string(`SshView.vue:500-504`);OSC 哨兵整行丢弃(`RemoteTerminal.vue:197-199`);SftpView 卸载不 resolve 冲突弹窗 Promise(`:389-404`);App.vue 背景 URL 直接拼 `url()` 无白名单(`:6`);批量删除无进度反馈。
- NIT:本地"在终端打开"硬编码 PowerShell;DashboardView 非空断言风格不一;SftpFileEditor Escape 常驻监听;SftpPane `computed<any[]>`;AgentFullTrace 无 seq 守卫;>1000 行的 5 个组件(DashboardView/ModelsPanel/GitReposPanel/AgentPanel/GitAccelPanel)行数主要是模板与样式,暂无强拆必要,首选拆出 ModelsPanel 的模型编辑对话框。

### Python 后端

- **[MAJOR] Windows 主机私钥缺口(代码已登记、仍未修)**(`backend/agent/harness/safety.py:283-289` 注释自认):`C:\keys\server.key`、`C:\data\deploy\host.pem` 这类任意路径密钥不命中 `_WIN_SECRET_PATH_RE`;`file_read` 本地腿前置校验只有 `is_traversal`。修法:补 `.pem/.key/.ppk/.p12/.pfx` 后缀 + `/etc/ssl/private` 前缀。
- **[MAJOR] `PermissionGate._confirmed_session` 无界增长**(`backend/agent/harness/reins/gate.py:197,584-588`):按 conversation 键控,仅 revoke 时删除,无 run 终态回收点(once/denied 池都有三重兜底)。修法:conversation 终结时顺带清理,或加 LRU 上限。
- **[MINOR] `secret_redact` 不覆盖 `Authorization: Basic`**(`backend/secret_redact.py:32-47`)与非 mysql 家族内联口令(`redis-cli -a`、`curl -u user:pass`)。
- **[MINOR] 沙箱 docker 后端 workdir bind 未约束**(`backend/agent/harness/sandbox_exec.py:70-74`):模型可控 `cwd="/"` 会把宿主根目录读写 bind 进容器(safe 命令免审批即触碰宿主任意 uid-1000 可写路径)。修法:cwd 白名单(HOME 下 + 临时目录)。
- **[MINOR] CORS `Origin: null` 与 `allow_credentials=True` 并存**(`backend/main.py:168-186`;已有 null origin 剥 Set-Cookie 补偿,纵深收紧项)。
- **[MINOR] `grant` 端点允许 hardline 命令进 allowlist**(`backend/routes/agent.py:1778-1830`):执行层确实拦得住,但权限面板会显示一条永不生效的"已预授权"。修法:grant 前跑一次 `classify_command`,hardline 直接 400。
- **[MINOR] 性能两处**:`event_stream` 的 `tool_call_start` 串行 await 在消费循环上(`agent.py:1109-1117`);`agent_run` setup 串行长链(意图→记忆→host_profile→goal→演化,`agent.py:715-938`)可 gather 压缩首字节时延。
- NIT:`PermissionGrantRequest.scope` 裸 str 可改 Literal;dev 模式 token 打印前 8 位仍是 2^32 泄露面;`Stirrups.undo` 单批回滚疑似遗留 API(仅 `undo_all` 被调用,待确认)。

### shared / gateway / scripts / 文档

- `gateway/hillgw.mjs`:整体扎实(拒绝清单 IPv6 逐字节比对、认证前帧白名单、占位防撞号、自比对 golden 字节)。两处可留意:**users[].passwordSha256 无盐 sha256**(配置文件泄露时彩虹表可破,内网工具可接受,建议在文件头登记取舍)与**认证无速率限制**。`:236` `portAllowed` 花括号同行属风格残留。
- `shared/gateway-mux.ts` 与 `hillgw.mjs` 双实现的防漂移机制(golden 字节 + CI selftest)运转正常。
- `scripts/`(ci-check.sh 五项门禁、clean.mjs 按形态识别产物、dev.cjs ELECTRON_RUN_AS_NODE 防御)无问题。
- 文档一致性:README 功能地图与实际入口一致;`backend/tests/README.md` 存在;CONTEXT.md 其余术语与实现核对成立(任务台双 store 边界严格、pendingPrefill 消费即清空、真就绪口径一致)。

---

## 总体评价

这是工程质量罕见地高的一仓:纯逻辑收敛 `shared/` 并配单测、修复带编号可追溯、注释普遍回答"为什么"、竞态守卫成体系(loadSeq/epoch/序列号守卫在各域齐备)、审计链达等保口径。四路深审均未发现 Critical。

剩余风险集中在三类:

1. **凭据规则覆盖面**(检测器规则表追不上真实世界的典型问题):本次补了云/包管理凭据族,主机私钥缺口(Windows 任意路径 `.pem/.key`)仍待补。
2. **流生命周期的"停滞"维度**:项目对"会崩的连接"防得很足(错误分类、fail-closed、断线重连),对"不崩也不动的连接"(SSE 黑洞、无看门狗)还没有统一水位。
3. **约定靠注释维持的模式**:500ms 握手被第二处照抄、三份手写右键菜单、`Skill` 同名异形——抽象还差"最后一公里"沉淀成代码。
