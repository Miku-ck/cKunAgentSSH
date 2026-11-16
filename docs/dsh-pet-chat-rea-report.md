# dsh-pet 对话功能逆向调查报告

> analyse Target: `dsh-pet` v0.4.9（MIT 开源），本地安装副本  
> 目标路径：`C:\Users\ckun\.dsh\profiles\desktop\node_modules\dsh-pet`  
> 分析方式：源码直读 + 静态证据固化（未执行运行时捕获）  
> 用途：功能学习与研究，用于重建自有 tab bot 的同类对话能力  
> 报告日期：2026-11-16



---

## 附录 B：落点评估（用户确认目标为 cKunBot 陪聊人格后的结论）

用户确认"要复刻的对话是给 cKunBot 陪聊人格用"后，我对本工作区做了第二轮证据核查（读 `backend/bot/chat_service.py`、`stickers.py`、`relation.py`、`database.py:4477`、`llm_adapter.py`）。**结论修正：不建议移植，cKunBot 已具备且更强。** 逐条证据如下：

| 候选设计点 | dsh-pet 做法 | cKunBot 现状（证据） | 结论 |
|---|---|---|---|
| 动作标记 `[动作:名]` | 注入提示词 + 白名单解析 + 回放回填 | 纯文字陪聊（local/QQ/微信），**无动画表演载体**；同类已有表情包标记 `[表情:开心]`（`stickers.py`） | 不适用，无落点 |
| 配图/标记的"回放回填" | 记忆只存名字，回放时回填 `[图:名]` | 表情包标记**原样落库**（`stickers.py:87-106` normalize 保留 `[表情:key]`），历史回放天然可见 | 已具备，无需补 |
| 历史时间跨度感知 | 超 30min 给 user 加 `[月日 时:分]` 前缀 | 已有 `relation.gap_days()` + `detect_situation()` 返回 `neglect`（久未联系，`relation.py:117`/`character.py:140`），且 `get_recent_bot_messages` **不返回 created_at**（`database.py:4477`），补这个要改 database 层 | 已有等价机制，改动不划算 |
| 第三方记忆扩展点 recall/capture | 单槽可插拔后端 | 已有成熟记忆系统：`memory.recall` 融合召回 + 三重门控、`remember_async` 落库、`scope=persona:{id}` 隔离（`chat_service.py:409-462`） | 已具备且更强 |
| 模型候选回落 | 配置模型失败 → 回落当前模型 | `llm_adapter` 有 SDK 重试 + BadRequest 降级（去 tools / 去 thinking 参数重试，`llm_adapter.py:306-322`） | 已具备 |
| 上下文四段式拼装 | 人设→名字→时间→【历史】 | `_system_prompt()` 分节编译，含身份/人格/关系/状态/分寸/宪章压尾（`chat_service.py:61-180`），比 dsh-pet 更细 | 已具备且更强 |

**最终决定**：不修改 `chat_service.py`，不新增对拍测试。理由——六项逐条核实后，没有任何一项是 cKunBot 缺失且值得移植的；盲目照搬会造成重复建设、破坏现有 `scope` 隔离与安全宪章，并引入回归风险。这符合"静态推断不夸大、证据先行"的分析原则。

若用户后续想要的是**桌宠形态**（透明窗 + 动画链 + 气泡 UI，而非纯文字），那属于另一个产品维度，需重新立题评估前端 Electron 的图形层，不在本次"对话功能"范围。

---

## 〇、结论摘要（先读这段）

dsh-pet 的"对话"本质是 **一条"取历史 → 拼上下文 → 流式生成 → 落盘记忆 → 写状态叶子"的管道**，分两种运行模式：

| 模式                   | 目录                | LLM 通道                                        | 复用价值                |
| -------------------- | ----------------- | --------------------------------------------- | ------------------- |
| **DSH 内（host）**      | `src/host/`       | 宿主注入的 `ctx.llm`（dsh-llm 抽象层，`BlockAssembler`） | 中（依赖宿主，逻辑可借鉴）       |
| **独立模式（standalone）** | `src/standalone/` | 自带 OpenAI / Anthropic SDK 流式客户端               | **高（可直接移植到你自己的项目）** |

对你要在自己的 tab bot 里实现对话，**最值得抄的是 `standalone/` 那一套**——它不依赖任何宿主，自带端点白名单、鉴权头重建、流式限额、错误归一；而 host 侧的上下文组装、记忆机制、动作/配图标记设计则是"对话体验"的灵魂，也应一并移植。

关键洞察（都有证据，见第二节）：

1. **上下文 = 四段式 system + 带标记的 history**：system 按"人设正文 → 名字声明 → 时间背景 →【历史】说明"固定顺序拼装；历史里 user 消息超过 30 分钟才加时间前缀，assistant 消息回填 `[动作:名]` / `[图:名]` 标记。
2. **记忆存名字不存素材**：配图/动作在记忆里只存名称字符串，回放时以文本标记回填给模型——既省 token 又让模型记得自己演过什么。
3. **记忆写入是串行队列**：read-modify-write 全程加进程内锁，防多端交错写盘。
4. **模型候选回落链**：配置模型优先 → 当前模型兜底，失败留日志；只有模型声明支持 reasoning 才传 `reasoningEffort: 'off'`。
5. **独立模式有第三方记忆扩展点**：`recall`（生成前改内容，1.5s 超时降级）/ `capture`（落盘后异步接收），单槽后注册覆盖。

---

## 一、功能解释

### 1.1 完整调用链（一条用户消息的一生）

```
HTTP POST /chat {text, attachment?}
  │  [routes/chat.ts] 校验：pet 存在、text 非空≤2000字、图片 base64/白名单
  ▼
chatWithPet(petId, text, image)          [plugin/chat-memory.ts]
  │  ① currentEnvironment() —— 城市+天气（锁外查，避免堵队列）
  │  ② withMemoryLock 串行队列内：
  │     ├─ readAllConfig → findPetInstance
  │     ├─ readMemory() 读 memory.json
  │     ├─ sliceMemoryRounds(messages, rounds) 截尾 N 轮（1轮=1问1答）
  │     ├─ petSystemPrompt() → composeSystemPrompt() 拼 system
  │     ├─ recallPatch() 第三方记忆召回（1.5s 超时降级）
  │     ├─ generateChat() ↓
  │     ├─ entry.messages.push(user + assistant) → writeMemory() 落盘
  │     ├─ captureTurn() 第三方记忆接收（不等待）
  │     ├─ state.writePet(petId,'anim') 有动作时
  │     └─ state.writePet(petId,'say') 写回复叶子→前端弹气泡
  ▼
generateChat()                          [host/chat.ts]
  │  modelCandidates() 候选链：chatModel 优先 → 当前模型兜底
  │  for 每个候选：generateWith()
  │     ├─ 历史 → createUserMessage / createAssistantMessage
  │     │     user：>30min 加 [月日 时:分] 前缀
  │     │     assistant：正文 + [动作:名]\n[图:名] 回填
  │     ├─ supportsReasoningOff() 探测能力
  │     ├─ 组装 content：userText + 配图指令 + 动作指令 + 可选图片块
  │     ├─ options: {provider, model, sessionId:'dsh-pet', messages, system,
  │     │            temperature:1, reasoningEffort?:'off', signal:60s超时}
  │     ├─ for await (chunk of llm.stream(options)) → BlockAssembler.push()
  │     └─ 提取 text → extractChatAction() / extractChatImage() 剥标记
  ▼
返回 {ok:true, text, image?, action?}
```

### 1.2 上下文组装（system 提示词，四段固定顺序）

证据 `host/persona.ts:58-69`。最终 system = 以下各行 `join('\n')`，空行自动跳过：

1. **人设正文**：实例级 `whisperPrompt` 优先，否则条目级；两者是**替换**不是拼接（避免出现两个身份）。
2. **名字声明**：`你的名字是"X"。`（name 空则用 id；都空则整段省略）
3. **时间背景**：`timeContextOf(now, env)` 现算（时段/星期/节日 + 城市天气），不缓存；注入 `Date` 由调用方给。
4. **【历史】说明**（仅对话且有历史时追加）：告诉模型历史里主人话前的 `[月日 时:分]` 是发生时刻，用来算时间跨度，且**不要照抄成自己的回复格式**。

### 1.3 历史消息的两种回填（都是防"模型记不住"的设计）

证据 `host/chat.ts:58-83`：

- **user 时间前缀**：`nowMs - ts > 30min` 才加 `[10月31日 22:53]`。只标 user 不标 assistant —— 标在 assistant 上模型会照抄（源码注释记录了真实 bug：回复以 `[11:54]` 开头）。整批历史共用一个 `nowMs`，避免逐条取时钟导致不一致。
- **assistant 标记回填**：`[动作:名称]` + `[图:名称]` 各占一行、顺序固定。因为落盘前正文里的标记已被剥掉，不回填模型就不知道自己前几轮演过什么，用户说"换一个"时无从判断。

### 1.4 配图与动作（可选，靠清单非空开关）

证据 `host/chat.ts:85-93`、`host/chat-action.ts:41-77`：

- 把候选清单**写进 user 正文末尾**（"从清单里挑，挑中在结尾另起一行写 `[图:名]` / `[动作:名]`"），让模型按语境选而不是随机抽。
- 顺序固定"配图在前、动作在后"。
- 回复里所有标记都会被正则剥掉（包括无效名称），只在命中白名单时采纳**第一个**；剥完压掉空行再弹气泡。

### 1.5 模型与生成

- **候选链**（`host/model-selection.ts:45-58`）：配置模型 → 当前模型，去重；全空则回 `provider-missing`。
- **reasoning 处理**（`host/llm-reasoning.ts:24-33`）：`resolveModelInfo()` 查到模型声明支持 `off` 才传 `reasoningEffort:'off'`；否则省略。因为对无 reasoning 元数据的模型显式传 off 会被判 `UNSUPPORTED_REASONING_EFFORT`，折叠成空流。
- **超时与错误**：单次生成 60s（`AbortSignal.timeout`）；失败返回结构化原因（`provider-missing` / `generate-error`），不伪造文案。无正文时按流终止原因翻译成人话（`aborted`→"生成已取消"，`max-tokens`→"输出达到上限"，等，见 `host/llm-call.ts:21-30`）。
- **不显式限 maxTokens**：推理模型把思考计入预算，显式小上限会截断正文。

### 1.6 记忆存储结构

证据 `host/plugin/chat-memory.ts:72-112`：

- 文件：`memory.json`，双层桶 `{ <条目assetRoot??petId>: { <实例id>: { messages: ChatMemoryMessage[] } } }`。
- 唯一读写方是 host 进程，浏览器/桌面两端都是客户端 → 天然共享同一份记忆。
- `withMemoryLock` 用 Promise 链把 read-modify-write 串行化。
- 每条消息字段：`role / content / ts / image? / meme? / action?`；同一轮问答共享同一个 `ts`（删除时按 ts 成对删）。
- 坏数据防御：`messages` 不是数组按新会话处理并记日志；解析失败不备份不改写源文件。

### 1.7 独立模式（standalone）—— 你最该复用的部分

证据 `standalone/ai-transport.ts` + `standalone/ai-provider/*`：

- **双协议**：OpenAI 兼容（`POST {base}/chat/completions`）与 Anthropic（`POST {base}/v1/messages`），走官方 SDK 流式。
- **端点白名单**（`isolatedFetch`）：SDK 拼出的 URL 必须逐一命中配置推导的预期地址，否则拒绝发出（`redirect:'error'`）。
- **鉴权头重建**：所有 header 由本层重建，不沿用 SDK 从环境/配置目录推断的凭据；Anthropic 显式 `authToken:null` 阻断 OAuth 解析。
- **关掉 SDK 自带行为**：`maxRetries:0`、静默 logger、`logLevel:'off'`。
- **流式限额**（`stream.ts`）：事件数 ≤4096、累计输出 ≤16384 字符、单次输入 ≤32768 字符、消息数 ≤64、末条必须 user；`refusal` 立即抛错。
- **同一时刻只有一个在途请求**（`runtime.active`），忙时直接拒绝；超时/取消经 `AbortController` 贯穿到底。
- **失败归一**：SDK 错误（可能含地址/头/正文）只进 `cause` 与日志，对外只透出固定文案；鉴权失败置 `runtime.paused`。

### 1.8 第三方记忆扩展点

证据 `host/chat-memory-provider.ts`：

- 单槽注册（两个 recall 同时改内容会冲突），`id` 不能占用 `'native'`，返回 disposer。
- `recall(input)` → 返回 `ChatContentPatch {system?, history?, userText?}`，只覆盖返回的字段；超时/抛错/非法形状一律"不修改"。
- `capture(input)` → 落盘后异步接收，不等待、失败只记日志。

---

## 二、证据清单

分析对象：npm 发布包 `dsh-pet@0.4.9`（MIT）。**该包无 `.git` 元数据**，故以文件路径 + 行号 + SHA-256 作为证据定位。以下哈希均为本地副本实算（取前 16 位）。

| #   | 断言                                    | 证据位置                                          | SHA-256(16)        |
| --- | ------------------------------------- | --------------------------------------------- | ------------------ |
| E1  | 对话生成主流程、候选回落、流式收集、标记解析                | `src/host/chat.ts:123-257`                    | `ecc43b8c99b4f179` |
| E2  | 历史时间前缀阈值 30min、标记回填规则                 | `src/host/chat.ts:44-83`                      | 同上                 |
| E3  | 记忆读写、串行队列、`chatWithPet` 全链路、第三方钩子接线   | `src/host/plugin/chat-memory.ts:63-481`       | `635b1617d4a07241` |
| E4  | 四段式 system 拼装、实例/条目人设替换               | `src/host/persona.ts:28-69`                   | `a99b3f014c3361b1` |
| E5  | 模型候选链、configuredModel                 | `src/host/model-selection.ts:20-58`           | `29939b01bd843d6a` |
| E6  | 动作指令注入与标记解析（白名单首个）                    | `src/host/chat-action.ts:41-77`               | `04beab3e4fa549da` |
| E7  | 第三方记忆单槽注册表、recall/capture 契约          | `src/host/chat-memory-provider.ts:39-103`     | `96237a696dee71bf` |
| E8  | 调用身份 `dsh-pet`、无正文失败原因翻译              | `src/host/llm-call.ts:14-30`                  | `809d1fc64c4ee007` |
| E9  | reasoning off 能力探测与保守降级               | `src/host/llm-reasoning.ts:24-33`             | `d955d7d6d2517710` |
| E10 | POST/GET/DELETE /chat 路由、图片准入、2000字限制 | `src/host/routes/chat.ts:19-163`              | `b33435b6f7da5c04` |
| E11 | 独立模式双协议、端点白名单、鉴权头重建                   | `src/standalone/ai-transport.ts:36-149`       | `2a6e49d073fc106b` |
| E12 | 独立模式流式限额、忙状态、错误归一                     | `src/standalone/ai-provider/stream.ts:14-90`  | `a46811c98f3c68f6` |
| E13 | 请求校验（末条user、zod schema、取消）            | `src/standalone/ai-provider/request.ts:18-62` | `7b60b5c12a2f1609` |
| E14 | 限额常量（32768/16384/4096/64）             | `src/standalone/ai-provider/limits.ts:13-38`  | `3cb96a4373c5d4d3` |

辅助佐证（未逐一算哈希）：`src/host/whisper.ts`（碎碎念，与对话共用 system 和生成通道）、`src/host/time-context/`（时段/节日/历史时间戳）、`src/standalone/context.ts`（独立模式上下文）、各 `*.test.ts`（行为规格，见 4.3）。

---

## 三、局限与未知

1. **静态推断 ≠ 运行时事实**：以上结论来自源码静态阅读，**未执行运行时捕获**。消息的实际时序、流式分块边界、模型真实返回形状、记忆并发下的实际行为，均未经运行时验证。
2. **静态图谱是语法观察**：若后续用 REA `analyze-javascript-application` 生成图谱，其为"保守的语法观察 + 候选关系"，不证明运行时行为。
3. **样本单一**：只分析了本地 v0.4.9 一个版本。仓库近期提交显示对话/记忆/服务商配置在持续重构（如 `service + keys + profiles` 三表、opencode-go 的 `x-opencode-session` 头），结论可能对其他版本不成立。
4. **未读取敏感/运行数据**：`logs.txt`、`memory.json`（运行数据）、`.credentials.yaml` 均未读取。真实协议交互、密钥形态、实际历史内容未纳入分析。
5. **未知项（unknowns）**：
   - 独立模式 `settings-page/` 的设置持久化细节未展开（对重建非核心）。
   - 语音朗读/语音输入链路（TTS/ASR）未纳入本次"对话"范围。
   - `x-opencode-session` 等第三方服务商特有头的完整清单未穷举（仓库新提交提到，但本地副本未必含）。
   - `partial` 覆盖：本报告是"单个功能路径"分析，不是整个应用的完整逆向。

---

## 四、重建建议（迁移到你的 tab bot）

> 前提：你的工作区 `D:/workspace/cKunAgentSSH` 已是 Electron + Node/TS 项目，天然适合直接移植 standalone 路径。

### 4.1 建议架构（照搬分层，别照搬耦合）

```
chat/
  transport.ts     # OpenAI/Anthropic 流式客户端 + 端点白名单 + 鉴权头重建（≈ E11）
  stream.ts        # 在途锁、限额、错误归一、取消贯穿（≈ E12）
  request.ts       # zod 校验、末条user、取消（≈ E13）
  context.ts       # system 四段拼装 + 历史回填（≈ E4 + chat.ts 的回填逻辑）
  memory.ts        # 读写、串行队列、rounds 截断、成对删（≈ E3）
  provider.ts      # 第三方记忆 recall/capture 扩展点（≈ E7，可选）
  action.ts        # 动作/配图标记注入与解析（≈ E6，按你的 UI 裁剪）
```

### 4.2 关键实现要点（照抄这些"坑位"，能省大量调试）

1. **上下文**：system 固定四段顺序；人设是替换不是拼接；时间 `Date` 由调用方注入（便于测试）。历史 user 超过 30min 才加时间前缀，且只标 user；assistant 回填你自己的结构化标记。
2. **记忆**：read-modify-write 必须串行；同一轮问答共享 `ts`；素材（图/动作）只存名字；`rounds=0` 时**不要写 `slice(-0)`**（JS 里返回整个数组），要显式判空。
3. **生成**：候选模型回落 + 日志留痕；只有模型声明支持才传 reasoning off；不显式设小 maxTokens；无正文时按终止原因给具体提示。
4. **传输**：端点白名单 + 鉴权头重建 + 关掉 SDK 重试/日志；同一时刻一个在途请求；输入端/输出端/事件数三重限额；错误归一（细节只进 cause/日志）。
5. **失败不伪造**：生成失败返回结构化原因，不编一句"友好文案"糊弄，否则上层无法区分降级。

### 4.3 行为对拍用例（用原项目的测试当规格）

原项目把关键行为都钉在 `*.test.ts` 里，把它们当作"原始行为规格"移植成对拍测试：

| 对拍点    | 参考测试文件                                                      | 断言什么                    |
| ------ | ----------------------------------------------------------- | ----------------------- |
| 历史时间前缀 | `host/chat-history-stamp.test.ts`                           | 30min 边界、只标 user、回复不带前缀 |
| 动作标记解析 | `host/chat-action.test.ts`                                  | 白名单首个、无效名也剥、剥后压空行       |
| 记忆轮次切片 | `host/chat.test.ts` / `shared/*`                            | rounds=0 空历史、截尾 N 轮     |
| 第三方记忆  | `host/chat-memory-provider.test.ts`                         | 单槽覆盖、超时降级、disposer      |
| 图片准入   | `host/routes/chat-image.test.ts`                            | 非法 base64/媒体类型拒绝且不调用生成  |
| 独立模式传输 | `standalone/ai-provider.test.ts`、`request-security.test.ts` | 端点白名单、鉴权头重建、限额          |
| 流式限额   | `standalone/ai-provider/stream.ts` 对应测试                     | 事件数/字符数上限、refusal、忙状态   |

做法：同一组输入（固定 system + 固定 history + 固定 userText）分别喂给"移植后的实现"和"录制的原始输出"，比对：最终发给模型的 messages 结构、剥标记后的正文、记忆写入的轮次与字段。

### 4.4 范围裁剪建议

桌宠的动画链、物理、素材生成链（webm/GIF）、余额轮询、天气/IP 定位、语音 TTS/ASR 都与"对话功能"无关，**建议整体排除**以收敛范围。若你的 tab bot 需要"角色感"，只需保留 `persona.ts` 的人设拼装 + 可选的 `[动作:]` 标记（映射成你自己的 UI 行为即可）。

---

## 附：复现本次分析的最小命令

```bash
# 目标核心源码（本地副本）
cd "C:/Users/ckun/.dsh/profiles/desktop/node_modules/dsh-pet/src"

# 证据哈希（报告中的 SHA-256）
sha256sum host/chat.ts host/plugin/chat-memory.ts standalone/ai-transport.ts

# 若需 REA 静态图谱（首次会安装 REA；Node 22+ 已就绪）
npx rea doctor --json
npx rea analyze-javascript-application "C:/Users/ckun/.dsh/profiles/desktop/node_modules/dsh-pet" --json
```
