# CONTEXT — 术语表

只放"这个词在这个项目里到底指什么"。实现细节看代码，取舍理由看代码注释与 git history。

| 术语 | 定义 | 为什么要钉住 |
| --- | --- | --- |
| **端口已分配**（`getPort()`） | spawn 后端**之前**从 8010–8100 里选定的端口号。非零只代表"选过端口"，不代表"有人在那个端口监听" | 把它当就绪用就是假 ready 病的病灶：后端彻底没起来时首屏仍显示"已就绪" |
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
