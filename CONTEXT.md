# CONTEXT — 术语表

只放"这个词在这个项目里到底指什么"。实现细节看代码，取舍理由看代码注释与 git history。

| 术语 | 定义 | 为什么要钉住 |
| --- | --- | --- |
| **端口已分配**（`getPort()`） | spawn 后端**之前**从 7777–7877 里选定的端口号。非零只代表"选过端口"，不代表"有人在那个端口监听" | 把它当就绪用就是假 ready 病的病灶：后端彻底没起来时首屏仍显示"已就绪" |
| **真就绪**（`whenReady()` / `ready`） | 后端已通过 `/api/health` 健康检查、可以接请求的状态 | 渲染层的状态机只能由真就绪驱动；端口号至多当 URL 提示用 |
| **降级模式**（degraded single-instance） | 拿不到单实例锁、但已证实是 userData 不可写（而非真有第二个实例）时继续启动，此时**单实例保证失效** | 与"已有实例→退出"是两种语义；两者的判据必须同源，否则会退回"打印继续启动却什么都不做"的僵尸分支 |
| **内嵌运行时**（bundled runtime） | 构建期把 Python 解释器随包放进 `resources/python-runtime/`；Windows 认 `runtime/python.exe`，POSIX 认 `runtime/bin/python3` | 两侧路径口径不一致过 —— 探到却读不到，mac/linux 等于从来没有内嵌运行时 |
| **孤儿后端**（orphan backend） | 上次应用异常退出后残留的 Python 后端进程，持有过期的 bridge 端口/token | 它的清理判据有宽/严之争，取舍记在 `electron/backend-manager.ts` 的 `killOrphanBackends` 注释（粗筛 + 二次甄别 + 按端口签名剔除自家）与 git history |
| **半写损坏**（torn write） | 进程被杀在写文件中间，留下半个 JSON | 项目内所有落盘必须 tmp+rename/os.replace 原子写，就是为了不产生这个词 |
| **隔离**（quarantine，`.corrupt-<ts>.bak`） | 读不出可信内容的文件被改名留证，而不是被静默当空集用 | "静默当空集"会让下一次写盘用子集覆盖真实基准；读不出必须诚实上报"不知道" |
| **审计结论口径**（`conclusion ∈ {…}`） | 后端枚举、前端筛选下拉、请求体类型三处必须同一份结论取值 | 三处同源的守卫钉在 backend/tests/test_ai_loop_core_regression.py |
| **任务台**（首页） | 首页的完整智能体对话工作区：消息流 + 内联执行过程 + 输入区；它是主对话面，不是启动器 | “首页”与“任务台”指同一个屏；再另造一个对话页就会变成两套入口 |
| **会话**（conversation） | 任务台里的一个历史单元，可包含多轮任务；点“新建会话”先进入空白任务，发出第一条消息才落库进历史 | 与“任务”“运行”区分：一次 run 是一轮执行，会话可以有很多轮 |
| **执行过程**（inline process） | 某个助手回合内的可折叠步骤流：计划、工具调用、审批、结果。运行中展开，完成后收起成摘要 | 过程属于那一轮回答，不属于独立页面或标签；外置就会与消息割裂 |
| **目标主机**（target host） | 下一次执行实际落到的 SSH 会话，由输入区目标 chip 切换，后端按 `activeSessionId` 落地 | 目标可以逐条消息变化，放在会话头部会谎称整段会话只有一个目标 |
| **历史弹窗**（`HomeHistoryDialog`） | 首页左上角“历史记录”按钮打开的居中弹窗，列出历史会话 | 曾是滑出抽屉、`DESIGN.md` 禁抽屉的唯一例外；改为居中弹窗后例外取消，按钮与弹窗都挂在 `HomeWorkspace.vue` |
| **问 AI 预填**（pendingPrefill） | 终端选区 / 批量下发结果 / 定时任务失败诊断送进任务台草稿的那一次请求，消费一次即清空 | AI 只有任务台一个界面，所以预填 = 切回首页 + 填输入框；留着它当第二个入口就会又长出一块面板 |
| **任务台双 store** | `stores/ssh-ai.ts` 管会话与消息（唯一对话面），`stores/agent.ts` 管单次 run 的执行态（SSE 事件流 / 工具调用 / 中止 / 成本） | 两个 store 名字都像"agent 相关"，混过一次就会把消息组装写进执行态 store，回放与持久化两边漂移 |
| **Bridge 双模块** | `agent/bridge_relay.py` 是 `/ws/bridge` 通道服务端（持连接 / 令牌握手 / 帧路由），`agent/electron_bridge.py` 是调用方客户端（拼请求 / 分类错误 / fail-closed） | 业务代码只 import 客户端；直接碰 relay 会绕过错误分类与"在途请求绝不重发"的保证 |
| **测试文件名** | `backend/tests/` 文件名只写守什么（`test_<域>_<守卫什么>.py`），不写轮次 / 工单号；出处历史在文件 docstring 与 git log，旧名对映见 `backend/tests/README.md` | 带 `_round13c` / `_s407` 后缀的名字看文件名猜不出守什么，只能靠 docstring——2026-10-24 已统一改名 |
| **剧本**（Runbook，`runbooks` 表） | 结构化的可执行运维剧本：步骤 = 命令 + 说明（≤20 步），经逐步无头 run 执行、标记行确定性判读 | 与 `runbook_save/search` 沉淀在 `memories` 里的「Runbook 经验文本」是两回事——一个可执行、一个可检索；混用会找错存储 |
| **cKunBot**（`bot/` 包，Bot 页） | 虚拟陪伴角色子系统：人格 / 表达 DNA / 关系 / 状态轴 / 记忆 / 主动性 / 多渠道。与运维 Agent 零打通（不调工具、不执行命令），只共用模型配置与 `memories` 表 | 它和首页任务台是两个东西：任务台是运维 Agent 的唯一对话面，cKunBot 是独立人格的陪聊；混用会把「帮我运维」的话术喂给陪聊人格 |
| **人格记忆**（`scope=persona:{id}`） | cKunBot 落在 `memories` 表里的事实，`kind=persona_fact`；Agent 侧只读 global / host:*，两者互不可见 | 共表不同 scope：衰减 / 淘汰 / 遗忘的 SQL 必须限定 scope+kind，碰宽了会删到 Agent 的记忆 |
| **关系阶段**（唯一口径 = score） | 陌生/熟悉/亲近/默契，由 relation 的 score（只增 + 读时衰减）按升温档缩放阈值推导；UI 进度条与 prompt 同源 | 曾有第二套 intimacy+trust 双门口径与它打架（同一份 prompt 两个阶段）；已删，阶段只认 relation |
| **状态轴**（`bot/state.py`） | trust/intimacy/tension/novelty/valence/energy/patience 七轴，「事件→增量」确定性推导，只喂档位词不喂裸数字 | 0 次额外模型调用是它的根基：可重放、可单测；改成让模型自报状态就全丢了 |
| **pacing**（在场感节奏） | 后端 `state.pacing_hint` 给的 SSE 提示：hesitate / relief / eager（空 = 正常） | 前端只认这三个值演动画，不自己猜状态；新增节奏要后端先给 |
| **表情包标记**（`[表情:开心]`） | 模型独占一条输出的文本标记；`bot/stickers.py` 校验键并收口为「一轮最多一张」，落库仍是标记 | 本地渲染内置 PNG、QQ 上传富媒体、微信按公网地址发图（失败回落颜文字）；行内出现的标记一律删除，绝不当作图 |
| **渠道 provider**（`bot/provider.py`） | 一个平台一个实现，5 成员协议（start / outbound_parts / fallback / name / reply_window_s） | 入站管线永远拿不到平台原始对象；接入新平台 = 写 provider + 进注册表，不改管线 |
| **寄存**（proactive queued） | QQ 被动回复窗口发不出的主动「心里话」，写 `bot_proactive_log(action=queued)` 存 48h，等对方来消息以「对了，」捎上 | 协议上 QQ 无法主动发消息；任何「直接发」的实现都会被平台拒绝 |
| **README 双语**（`README.md` / `README.en.md`） | 中英两份 README 是同一份事实的两个语言投影：章节、表格、代码块、架构图逐节对应，任一侧改动功能 / 命令 / 架构都要同步另一侧 | 只更中文等于让英文版谎报功能与命令，「只有中文版」过期比没有英文版更误导 |
| **示例脱敏**（README / 代码注释 / UI 占位符 / 示例配置） | 公开面一律只写占位符：`your-llm-host`、`example.com`；真实内网地址、主机名与凭据只允许存在于本地 `backend/.env`（已被 `.gitignore` 忽略） | 文档与界面是公开面，真实内网 IP 写进示例等于给攻击者画拓扑图，还会被复制粘贴扩散 |
