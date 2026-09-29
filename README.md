# cKunAgentic

SSH 智能运维终端 — 集成 SSH 连接管理、远程终端、SFTP 文件传输与 AI 运维助手。深色 IDE 风格界面，统一字体（Segoe UI / SF Pro）与配色。

## 功能特性

- **SSH 连接管理**：密码 / 密钥 / Agent 认证，连接配置 safeStorage 加密持久化，导入/导出
- **远程终端**：ssh2 shell channel + xterm.js，支持 OSC 7 工作目录跟踪、LS_COLORS 优化
- **本地终端**：node-pty + xterm.js，支持 CMD / PowerShell / Git Bash / WSL
- **SFTP 文件传输**：断点续传（.part 临时文件）、分块并发、心跳检测、错误分类
- **远程文件编辑**：Monaco Editor 在线编辑远程文件
- **命令补全**：Tab 补全，通过 SSH exec 远程查询候选；历史命令 fuzzy 联想（字符子序列 + 词首/连续加分）+ 执行次数徽标
- **快捷命令栏**：自定义命令快捷执行
- **多节点批量下发**：对多台活动远程会话并行下发一条命令，结果并排对比 + 逐行差异高亮，可一键送 AI 分析；生产环境标记 / 危险命令命中时执行前强制二次确认
- **实时监控仪表盘**：每 5s 轮询远程指标（CPU 跨轮次差分 / 内存 / 负载 / 磁盘 / 进程 TOP / 监听端口 / GPU nvidia-smi），Sparkline 趋势，单点指标可「分析」送 AI；连续失败转后台重试，断线自动恢复
- **GPU nvtop 面板**：nvitop 风格逐卡视图（算力/显存/温度/功耗/进程），双层量值图（进度条=当前值、趋势薄条=60 格历史），矛盾态检测（占卡不干活 / IO 等待 / SWAP 已满）以条端标记+悬浮提示呈现；档位阈值全局统一为 60/80（负载 4/8）
- **终端「问 AI」**：选中终端内容 → 选区旁悬浮按钮 → 预填 AI 面板问题草稿，随消息自动注入 cwd / 最近命令 / 选区上下文
- **AI 运维助手**：流式对话 + 思考链、多模型切换、Agent 智能体（工具调用）
- **Agent 框架（Harness）**：ReAct 决策循环、13 拦截器、29 工具、动态步数预算、循环检测、成本熔断
- **Agent 记忆 v2**：单一写入方对账（去重/消解矛盾/失效）、混合检索（FTS5 + 向量）、常驻画像块、注入 token 预算；设置 → 记忆 可浏览/检索/置顶/编辑/失效记忆并查看对账队列与块历史
- **计划模式**：前端 mode chip 成对下发 `workflow_mode=plan` + `permission_mode=plan`，后者由 `PlanModeGuard` 在审批卡之前就把写操作硬拦成只读（不是提示词约定）；「计划批准后才放行执行」的门尚未接线，见 `docs/plan-mode.md` §6.2
- **澄清向导**：Agent 工具调用意图不明时拦截并弹出多步澄清卡片（A/B/C + 自定义），答完注入上下文放行、可跳过；精准调用零打扰
- **生产环境授权强化**：prod 环境仅认预授权 allowlist（禁临时审批），service stop/restart 等破坏性动作自动升级 dangerous 需逐次审批
- **审批卡片中文说明**：需审批命令自动附中文后果说明（静态后果表全覆盖），未知命令由 LLM 后台补发一句解释回填卡片；纯展示层，不参与任何放行/拦截判定
- **审批焦点队列**：并发审批请求按最旧优先单卡聚焦、+N 折叠可逐条切换，支持全部允许/全部拒绝；待办常驻（超时可配置，默认 600s 兜底），同命令重发复用待办不重复建卡；人工介入为顶部细条通知+汇总计数（不抢占工作区）
- **会话保留（可选）**：设置页可开启自动清理已完结会话（默认关闭，永不触碰进行中的会话）
- **目标机环境画像**：run 开始自动探测发行版 / init / 包管理器 / 身份，注入系统提示，避免模型凭印象生成与发行版不匹配的命令
- **运维方法论注入**：意图识别输出 `is_change`（变更 / 只读正交维度），按任务性质二选一注入变更纪律 / 只读取证方法论
- **定时任务**：倒计时、历史耗时、失败 run 一键「AI 诊断」
- **CI 门禁**：`scripts/ci-check.sh` + GitHub Actions 工作流（后端 pytest / 前端 typecheck / vitest / 构建）

## 技术栈

### 桌面端

| 层 | 技术 | 用途 |
|---|---|---|
| 桌面框架 | Electron 35 | 跨平台应用壳（Win / macOS / Linux） |
| 前端 UI | Vue 3 + Vite 6 | 渲染进程，SPA |
| 状态管理 | Pinia | 全局状态、会话管理 |
| 代码编辑器 | Monaco Editor | 远程文件在线编辑 |
| 终端模拟 | xterm.js + node-pty | 本地终端 |
| 远程终端 | ssh2 | SSH 连接、远程 shell、SFTP |
| 构建工具 | electron-vite | Vite + Electron 集成 |
| 打包 | electron-builder | 跨平台安装包 |

### 后端

| 层 | 技术 | 用途 |
|---|---|---|
| 框架 | Python 3.11+ / FastAPI | AI 能力层（对话、Agent、模型管理） |
| ASGI | uvicorn | 运行 FastAPI |
| LLM 客户端 | openai SDK | OpenAI 兼容协议（vLLM / Ollama / OpenAI） |
| 关系存储 | SQLite | 对话历史、会话状态、技能配置 |

## 架构总览

```
┌─────────────────── Electron 应用壳 ───────────────────┐
│                                                       │
│  ┌─ Renderer (Vue 3) ─┐    ┌─ Main (Node.js) ─────┐  │
│  │                     │    │                       │  │
│  │  SSH 终端 + AI 面板 │    │  node-pty (本地终端)  │  │
│  │  Monaco Editor      │    │  ssh2 (远程终端)       │  │
│  │  xterm.js           │    │                       │  │
│  │  Pinia              │    │  BackendManager       │  │
│  │                     │    │  (spawn python.exe)   │  │
│  │  ← HTTP / SSE →     │    │  (健康检查/端口管理)   │  │
│  │  ← Electron IPC →   │    │  (PTY/SSH 会话池)     │  │
│  │                     │    │        ↓              │  │
│  └─────────────────────┘    └─── localhost:port ───┘  │
│                                                       │
└───────────────────────┬───────────────────────────────┘
                        │
        ┌───────────────┴────────────────┐
        │     Python FastAPI Backend     │
        │  (uvicorn / 系统 Python 或包内  │
        │   runtime/ 内嵌嵌入式 Python)   │
        │                                │
        │  • Chat 流式对话 (SSE)          │
        │  • Agent Harness 框架          │
        │    - ReAct 决策循环            │
        │    - 13 拦截器 (Reins)         │
        │    - 29 工具 (Saddle)         │
        │    - 动态预算 + 成本熔断       │
        │  • 会话持久化 (SQLite)          │
        │  • 模型配置 / 路由             │
        │  • 技能系统 (SkillRegistry)    │
        └───────────────┬────────────────┘
                        │
                  vLLM (OpenAI 兼容)
```

**三条数据流：**

- **AI 对话**：Renderer → HTTP/SSE → Python FastAPI
- **终端 / SSH / SFTP**：Renderer → Electron IPC → node-pty / ssh2（纯 Node.js，不经 Python）
- **Agent 远程执行**：Python 后端 → WS 反向通道（`/ws/bridge`，主进程出站拨号、长期保持）→ Electron 主进程 dispatch → ssh2。断线 fail-closed，在途请求绝不重发（远程命令非幂等）

## 项目结构

```
cKunAgentSSH/
├── electron/                          # Electron 主进程
│   ├── main.ts                        # 入口，窗口管理，IPC handler 注册
│   ├── preload.ts                     # 安全桥接 API (contextBridge)
│   ├── backend-manager.ts             # Python 进程生命周期管理
│   ├── electron-bridge.ts             # 桥接业务实现（token 生成 + dispatch，经 WS 反向通道下发）
│   ├── pty-manager.ts                 # node-pty 终端实例池
│   ├── ssh-manager.ts                 # ssh2 连接池 + SFTP
│   ├── ssh-store.ts                   # 连接配置持久化存储（safeStorage 加密）
│   └── utils/
│       ├── path-guard.ts              # 路径安全校验（越界/Key 文件）
│       └── terminal-colors.ts         # LS_COLORS 配置
│
├── src/                               # Vue 3 渲染进程
│   ├── App.vue                        # 根组件：TitleBar + SshView
│   ├── main.ts                        # Vue 入口
│   ├── monaco-setup.ts                # Monaco 编辑器初始化
│   ├── env.d.ts                       # window.electronAPI 类型声明
│   │
│   ├── api/
│   │   └── client.ts                  # 后端 HTTP / SSE 客户端
│   │
│   ├── layouts/
│   │   └── TitleBar.vue               # 自定义标题栏 + 窗口控制
│   │
│   ├── views/
│   │   └── ssh/
│   │       ├── SshView.vue            # SSH 主视图：标签栏 + 终端 + AI 面板
│   │       ├── DashboardView.vue      # 监控仪表盘（CPU/内存/负载/磁盘/GPU/进程/端口）
│   │       ├── SchedulesView.vue      # 定时任务（倒计时 + 失败 AI 诊断）
│   │       └── AuditView.vue          # 审计记录视图
│   │
│   ├── components/
│   │   ├── common/                    # 通用组件（Toast / Confirm / Settings）
│   │   ├── agent/                     # Agent 运行面板 + 追踪可视化
│   │   ├── chat/                      # 聊天面板 + 消息渲染 + 历史管理
│   │   ├── icons/                     # SVG 图标组件
│   │   ├── settings/                  # 设置面板（模型 / 外观 / 聊天等）
│   │   └── ssh/                       # SSH 核心组件
│   │       ├── ConnectionModal.vue    # 新建/编辑 SSH 连接
│   │       ├── ConnectionRow.vue      # 连接列表行
│   │       ├── LocalTerminal.vue      # 本地终端（node-pty + xterm.js）
│   │       ├── RemoteTerminal.vue     # 远程终端（ssh2 shell + xterm.js）
│   │       ├── RemoteManager.vue      # 远程连接管理
│   │       ├── SftpPane.vue           # SFTP 文件传输
│   │       ├── SftpView.vue           # SFTP 文件浏览
│   │       ├── SftpFileEditor.vue     # 远程文件编辑（Monaco）
│   │       ├── SshAiPanel.vue         # AI 运维助手面板
│   │       ├── BulkCommandModal.vue   # 多节点批量下发 + 结果对比
│   │       ├── QuickCommandBar.vue    # 快捷命令栏
│   │       ├── CompletionPopup.vue    # 命令补全弹窗（含历史次数徽标）
│   │       ├── NoteInput.vue          # 备注输入
│   │       └── QuickCommandEditor.vue # 快捷命令编辑
│   │
│   ├── composables/                   # 组合式函数
│   │   ├── useAgentDeliver.ts         # Agent 结果交付（命令投递/状态回传）
│   │   ├── useAppearance.ts           # 主题/外观管理
│   │   ├── useChatPersistence.ts      # 聊天持久化
│   │   ├── useCompletion.ts           # 命令补全（含历史 fuzzy 联想纯函数）
│   │   ├── useConfirm.ts              # 全局确认对话框
│   │   ├── useModelSelector.ts        # 模型选择
│   │   ├── useResizablePanel.ts       # 可拖拽面板
│   │   ├── useSettings.ts             # 设置对话框
│   │   ├── useCommandDelivery.ts      # 命令交付/焦点管理
│   │   ├── useSshNavigation.ts        # SSH 导航与终端切换
│   │   ├── useTabContextMenu.ts       # 标签右键菜单
│   │   ├── useTerminalConfig.ts       # 终端配置
│   │   ├── useTerminalContext.ts      # 终端「问 AI」：选区登记 + 悬浮按钮 + cwd 同步
│   │   └── bulkCommandDiff.ts         # 批量下发结果对比 + 危险命令启发式（纯函数）
│   │
│   ├── stores/                        # Pinia 状态
│   │   ├── app.ts                     # 后端状态管理
│   │   ├── ssh.ts                     # SSH 会话池 + 连接配置（含终端选区/cwd 注册表）
│   │   ├── sftp.ts                    # SFTP 传输状态
│   │   ├── ssh-ai.ts                  # SSH AI 对话（含「问 AI」pendingPrefill 通路）
│   │   ├── agent.ts                   # Agent 运行状态
│   │   ├── server-monitor.ts          # 实时监控轮询编排（5s 采样 + 失败重试）
│   │   ├── server-monitor-core.ts     # 监控远端采集命令 + /proc 解析（纯函数）
│   │   ├── skills.ts                  # 技能管理
│   │   ├── quickCommands.ts           # 快捷命令
│   │   ├── settings.ts                # 应用设置
│   │   ├── models.ts                  # 模型配置列表
│   │   └── toast.ts                   # 消息提示
│   │
│   └── utils/
│       ├── id.ts                      # ID 生成
│       ├── language.ts                # 语言检测
│       ├── markdown.ts                # Markdown 渲染
│       ├── terminal-colors.ts         # ANSI 背景色过滤
│       ├── think-scrubber.ts          # 思考链清理
│       ├── format.ts                  # 展示格式化（字节数 / 后端 UTC naive 时间戳）
│       ├── menu.ts                    # 浮层菜单贴边时翻向另一侧
│       ├── run-modes.ts               # 姿态+审批档 → workflow_mode/permission_mode 成对下发
│       ├── finalizeAgentMessage.ts    # 固化运行结果到会话占位消息（store watch 调用）
│       └── nvtop-scale.ts             # GPU 面板取色与量值映射（阈值分档）
│
├── backend/                           # Python FastAPI 后端
│   ├── main.py                        # FastAPI 入口 + 路由注册
│   ├── config.py                      # 配置管理
│   ├── database.py                    # SQLite 操作层
│   ├── llm_adapter.py                 # OpenAI 兼容流式客户端
│   ├── prompts.py                     # 系统提示词构建
│   ├── post_processor.py              # 后处理（记忆/技能/标题生成）
│   ├── skill_registry.py              # 技能注册表
│   ├── builtin_skills.py              # 包内内置技能同步（skills_builtin/ → DB）
│   ├── skills_builtin/                # 随包分发的内置运维技能（SKILL.md）
│   ├── safe_tokens.py                 # Token 安全处理
│   ├── netconfig.py                   # 出站 HTTP 策略（代理/超时/重试）唯一读取点
│   ├── logconfig.py                   # 后端日志级别与 root handler
│   ├── sse_utils.py                   # SSE 工具函数
│   ├── requirements.txt               # Python 依赖
│   ├── requirements-dev.txt           # 测试依赖（pytest / pytest-asyncio）
│   ├── requirements-optional.txt      # 可选依赖（trafilatura 网页正文提取）
│   │
│   ├── agent/                         # Agent 智能体框架
│   │   ├── harness/                   # Harness 核心（马术装备隐喻）
│   │   │   ├── harness.py             # 组装入口
│   │   │   ├── driver.py              # ReAct 决策循环主驱动
│   │   │   ├── bridle.py              # 上下文组装 + 压缩
│   │   │   ├── saddle.py              # 工具执行 + 沙箱
│   │   │   ├── stirrups.py            # LLM 调用 + 重试
│   │   │   ├── reins/                   # 拦截器策略层（core/guards/gate/flow/lifecycle/clarification/plan_mode/auto_approve/defaults）
│   │   │   ├── safety.py              # 命令/路径/SSRF 安全防御
│   │   │   ├── command_explain.py     # 审批卡片中文后果说明（静态表 + LLM 兜底）
│   │   │   ├── telemetry.py           # 遥测总线
│   │   │   ├── contracts.py           # 契约定义
│   │   │   └── llm_client.py          # 流式 LLM 客户端
│   │   │
│   │   ├── memory/                    # Agent 记忆 v2（单一写入方对账 + 画像块）
│   │   │   ├── mstore.py              # 存储层：写入/检索池/置顶/失效/统计
│   │   │   ├── retriever.py           # 混合检索（FTS5 + sqlite-vec 融合）
│   │   │   ├── reconcile.py           # 对账器：候选 → 合并/取代/失效
│   │   │   ├── blocks.py              # Letta 式常驻画像块（含历史回滚）
│   │   │   ├── budget.py              # 注入预算分配（inject/blocks tokens）
│   │   │   ├── text.py                # 正文规范化与去重键
│   │   │   ├── vec.py                 # sqlite-vec 扩展装载与向量表
│   │   │   ├── embedding.py           # 向量化（失败静默降级为纯词法）
│   │   │   └── __init__.py            # 对外统一导入面
│   │   ├── workflow.py                # Spec/Plan/Goal 三阶段工件按会话落盘读写
│   │   ├── reflection.py              # 元反思
│   │   ├── evolution.py               # 策略演化
│   │   ├── intention.py               # 意图分类（含 is_change 变更/只读维度 + 逐轮引导）
│   │   ├── host_profile.py            # 目标机环境画像（只读探测 + 缓存 + 静默降级）
│   │   ├── audit.py                   # 审计引擎（链式签名）
│   │   ├── cost_tracker.py            # 成本追踪
│   │   ├── consciousness_tick.py      # 后台心智维护
│   │   ├── electron_bridge.py         # 桥接客户端（WS-only，经 bridge_relay 下发命令）
│   │   ├── bridge_relay.py            # WS 反向通道服务端（/ws/bridge 单连接中枢）
│   │   │
│   │   └── tools/builtin/             # 29 个内置工具（SSH 模式）
│   │       ├── shell_exec.py          # SSH 远程命令执行
│   │       ├── web_tools.py           # 联网搜索/抓取
│   │       ├── todo_write.py          # 任务列表
│   │       ├── load_skill.py          # 按需加载技能
│   │       ├── spawn_subagent.py      # 启动子 Agent
│   │       ├── memory_tools.py        # 记忆自编辑（主动查/记/作废，spec §7）
│   │       ├── workflow_tools.py      # spec/plan/goal 工件写入
│   │       ├── ops_common.py          # 运维共享执行辅助（双模式）
│   │       ├── ops_obs.py             # 只读观测（概览/进程/网络）+ 日志 + 环境画像模板
│   │       ├── ops_service.py         # 服务生命周期（systemctl）
│   │       ├── ops_schedule.py        # 计划/定时任务
│   │       ├── ops_file.py            # 文件操作（读/写/改/查找/搜索）
│   │       ├── ops_image.py           # 图片查看
│   │       └── ops_job.py             # 后台任务
│   │
│   └── routes/
│       ├── chat.py                    # 对话接口（SSE 流式）
│       ├── agent.py                   # Agent 运行接口（SSE + 工具调用）
│       ├── models.py                  # 模型配置查询
│       ├── settings.py                # 设置接口
│       ├── skills.py                  # 技能 CRUD
│       ├── terminal.py                # 终端信息接口
│       ├── schedules.py               # 定时任务接口
│       ├── workflow.py                # Spec/Plan/Goal 工件读写接口
│       └── audit.py                   # 审计记录查询/导出/校验
│
├── electron.vite.config.ts            # electron-vite 构建配置
├── electron-builder.yml               # 打包配置
├── scripts/ci-check.sh                # 本地一键 CI（与 GitHub Actions 检查项一致）
├── scripts/deploy/                    # 远端最小权限部署（白名单执行器等）
├── vitest.config.ts                   # 前端单测配置（node 环境）
├── package.json
└── tsconfig.json / tsconfig.web.json / tsconfig.node.json
```

## 核心模块说明

### Electron 主进程

- **main.ts** — 入口。窗口创建（无边框 + 自定义标题栏）、IPC handler 注册（pty/ssh/fs/sftp/win/backend:status）
- **preload.ts** — `contextBridge` 暴露受限 API，类型契约在 `src/env.d.ts`
- **backend-manager.ts** — Python 进程生命周期：spawn、健康检查、自动重启、端口发现
- **pty-manager.ts** — node-pty 实例池，跨平台 shell 检测
- **ssh-manager.ts** — ssh2 连接池，支持密码/密钥认证、shell channel、SFTP 断点续传
- **ssh-store.ts** — 连接配置持久化，safeStorage OS 级加密，原子写入
- **electron-bridge.ts** — 桥接业务实现：生成 token（注入后端 env + WS hello 共用），`dispatch()` 为 WS 通道 req 帧的唯一业务实现。阶段 2 起不再监听任何端口（HTTP 端口桥已移除）

### 渲染进程

- **App.vue** — 根布局：TitleBar + SshView，直接渲染 SSH 界面
- **TitleBar.vue** — 自定义标题栏：品牌标识 + 窗口控制按钮
- **SshView.vue** — SSH 主视图：Quick Start 首页 / 远程管理 / 本地终端 / SFTP / AI 面板 / 监控仪表盘 / 定时任务 / 快捷命令栏

### 状态管理

- **app.ts** — 后端状态（端口、健康检查）
- **ssh.ts** — SSH 会话池、连接配置 CRUD
- **sftp.ts** — SFTP 传输队列状态
- **ssh-ai.ts** — SSH AI 对话管理
- **skills.ts** — 技能加载与 @ 触发
- **settings.ts** — 应用设置（模型、外观、终端等）

### Python 后端

- **main.py** — FastAPI 入口，CORS 配置，路由注册
- **database.py** — SQLite 操作层：会话 CRUD、消息存储、模型配置
- **llm_adapter.py** — OpenAI 兼容流式客户端
- **routes/chat.py** — SSE 流式对话
- **routes/agent.py** — Agent 运行接口，含工具调用
- **routes/skills.py** — 技能 CRUD + 内置技能同步

## Agent 框架（Harness）

Agent 框架采用马术装备隐喻，契约驱动设计。

### 架构

```
Driver（驭手）—— ReAct 决策循环主驱动
  ├── Reins（缰绳）—— 13 拦截器策略层
  │   ├── SkillRein         AGENT_START    技能注入
  │   ├── LoopDetector      PRE_TOOL_CALL  循环检测
  │   ├── PermissionGate    PRE_TOOL_CALL  权限审批（prod 仅预授权 allowlist）
  │   ├── FilesystemGuard   PRE_TOOL_CALL  敏感命令检测 + 审批卡片中文说明
  │   ├── ClarificationInterceptor PRE_TOOL_CALL 澄清向导（意图不明时拦截提问）
  │   ├── PlanModeGuard     PRE_TOOL_CALL  计划阶段只读强制（priority 6，先于三张审批卡）
  │   ├── AuditLogger       POST_TOOL_CALL 审计日志
  │   ├── PlanDisciplineRein PRE_ITERATION 计划阶段行为纪律（priority 12）
  │   ├── DynamicBudgetRein POST_ITERATION 动态步数预算
  │   ├── CostBudgetRein    POST_ITERATION 成本/token 硬熔断
  │   ├── ConfidenceMonitor POST_ITERATION 置信度监控
  │   ├── ReflectionRein    AGENT_COMPLETE 反思演化
  │   └── ReflectionRein    AGENT_FAILED   失败反思（同组件双实例）
  │
  ├── Bridle（马缰）—— 上下文组装 + 压缩
  ├── Saddle（马鞍）—— 工具执行 + 沙箱（29 个内置工具）
  ├── Stirrups（马镫）—— LLM 调用 + 重试
  └── TelemetryBus —— 事件广播
```

### ReAct 决策循环

```
INIT → THINKING → [LLM 调用] → 有工具调用?
  ├─ 是 → ACTING → [工具执行] → OBSERVING → THINKING（循环）
  └─ 否 → COMPLETED
达到 max_iterations → MAX_REACHED
成本/token 超限 → CostBudgetRein 熔断 → FAILED
```

### 29 个内置工具

#### 基础工具

| 工具 | 风险 | 说明 |
|---|---|---|
| `shell_exec` | moderate | 命令执行（SSH 远程 / 本地子进程双模式，可见终端交付） |
| `web_search` | safe | 联网搜索（Tavily API，未配密钥/刮不到结果时降级 360→DuckDuckGo） |
| `web_fetch` | safe | 抓取网页（SSRF 防护） |
| `todo_write` | safe | 任务列表管理 |
| `load_skill` | safe | 按需加载技能完整指令 |
| `spawn_subagent` | safe | 启动子 Agent 处理独立子任务（纯推理） |

#### 运维观测与日志（只读）

| 工具 | 风险 | 说明 |
|---|---|---|
| `sys_overview` | safe | 系统概览：负载/内存/磁盘/内核/时间 |
| `process_inspect` | safe | 进程 TOP N 与按名查看 |
| `network_probe` | safe | 监听端口 / 网络连接 |
| `log_read` | safe | 受限日志读取（路径校验 + 行数/模式过滤） |

#### 服务与计划

| 工具 | 风险 | 说明 |
|---|---|---|
| `service_manage` | moderate | systemctl 服务操作（status/start/stop/restart，破坏性标记） |
| `schedule_op` | moderate | 定时任务管理（create/list/cancel：固定间隔 + 受限 cron） |

#### 文件操作（远程/本地双模式）

| 工具 | 风险 | 说明 |
|---|---|---|
| `file_read` | safe | 带行号、offset/limit 分页读取（拒绝 `..` 越界） |
| `file_write` | moderate | 新建/整体覆写（远程 base64 落盘 / 本地 Python） |
| `file_edit` | moderate | 精确替换（old_string 唯一校验；远程经 base64 读取避免内容误删） |
| `file_glob` | safe | 按路径模式递归查找文件 |
| `file_grep` | safe | 正则搜内容（远程 grep / 本地 Python re） |

#### 图片查看与后台任务

| 工具 | 风险 | 说明 |
|---|---|---|
| `read_image` | safe | 读 PNG/JPEG/WebP/GIF（白名单 + 10MB 上限），返回 base64 data URI |
| `job_run` | moderate | 后台启动长驻/跟随任务（local 本地子进程 / remote 远程 setsid 脱离会话，返回 job_id + PID） |
| `job_list` | safe | 列出全部后台任务（远程任务重启后从 agent_jobs 恢复，顺带 kill -0 存活对账） |
| `job_output` | safe | 读 job 输出（本地=内存缓冲 / 远程=增量 tail 日志 + 退出码文件，可 wait 阻塞） |
| `job_kill` | moderate | 终止后台任务（本地 SIGTERM→SIGKILL / 远程整进程组 TERM→KILL 升级） |

#### 记忆自编辑与三阶段工件

| 工具 | 风险 | 说明 |
|---|---|---|
| `memory_search` | safe | 主动检索自己的记忆库（与被动注入是双通道） |
| `memory_add` | safe | 显式记一条事实/经历/规程，进对账队列而非直接落池 |
| `memory_invalidate` | safe | 按 id 失效一条记忆（失效不是删除，保留可回滚历史） |
| `block_update` | safe | 改写常驻画像块（Letta 式，旧内容入历史） |
| `spec_write` | safe | 写规格工件 spec.md（按会话目录落盘，无 DB） |
| `plan_write` | safe | 写计划工件 plan.md |
| `goal_write` | safe | 写目标工件 goal.md（成功标准锚） |

> 文件/图片/后台任务均**双模式**：有 SSH 会话时操作远程服务器（POSIX），
> 无会话时操作本地主机（Python 原生 API，规避 Windows CMD 不认 POSIX）。
> 所有文件路径拒绝 `..` 越界，写/杀操作带风险标记与确认。
> 远程 job：cmd 经 base64 落脚本（防注入），setsid 进程组脱离会话（关窗口继续跑），
> 日志/退出码落 /tmp/ckun_agent_jobs/&lt;job_id&gt;/，注册表持久化 agent_jobs 表 —
> backend 重启可恢复、对账、kill；agent run 取消/断连时自动清理该 run 名下远程任务。
> job_run 的内部命令与 shell_exec 同走命令级安全分类与审批（wrapper 不参与分类）。

### 安全防御

- **安全边界与威胁模型**：见 [`docs/security-boundary.md`](docs/security-boundary.md) —— 四层判定与处置、高危命令口径、通道分级（AI 自主 / 人授权 / 人自主）、授权键语义、已知取舍登记
- **命令安全**：唯一判定源 `safety.classify_command`（6 步归一化抗混淆 + hardline 阻断 + dangerous 逐条审批 + 未入只读白名单即审批）；能自拼命令的工具（`file_write`/`file_edit`/`schedule_op` 等）经 `bound_command` 报出真实执行命令，与 `shell_exec` 同口径判定，授权键绑定命令/路径哈希。判定精度：电源动词按**执行位置**命中（`grep -i reboot` 不再误拦）、凭据读取按**路径**命中（`sort /etc/shadow` 不再漏放）、只读白名单动词的写开关与 curl/wget 上传侧外传一律要审批 — 误拦/误放双向回归见 `backend/tests/test_safety_misjudgment.py`
- **SSRF 防护**：IP 拒绝私网 + DNS rebinding 防护 + 重定向校验
- **远端最小权限（部署侧）**：`scripts/deploy/` 提供受限账户一键部署（`setup-ckun-agent.sh`）+ 白名单执行器（`agent-exec.py`，22 模板逐条锚点匹配、默认拒绝、base64 洞递归封堵）+ 部署验证（`ckun-agent-verify.sh`）。Agent 在远端默认只能执行白名单内命令，与代码侧 HARDLINE/审批形成纵深；跨端一致性由契约测试 `test_remote_whitelist_contract.py` 钉死（改任一端命令格式必须跑该测试）

## 开发

### 前置条件

- Node.js 20+
- Python 3.11+
- 可访问的 OpenAI 兼容 LLM 服务（vLLM / Ollama / OpenAI）

### 首次启动的模型配置

首次启动**不会预填任何模型**（模型列表为空），请在 设置 → 模型 中添加你的 LLM 端点。

开发者/演示环境若希望保留"首启自动探测"，可在 `backend/.env`（或进程环境变量）中显式开启：

```bash
# 必填：首启探测 {VLLM_BASE_URL}/models，取第一个模型自动种子为默认模型
VLLM_BASE_URL=http://192.168.150.40:9030/v1
# 可选：再探测并种子一条思考模型（supports_thinking）
VLLM_THINKING_URL=http://192.168.150.40:9031/v1
# 可选：API Key（本地 vLLM 默认 EMPTY）
VLLM_API_KEY=EMPTY
```

探测失败（服务未启动/无模型）时不种子、不阻塞启动；服务就绪后重启应用会自动重试。

### 启动

```bash
# 安装前端依赖
npm install

# 安装 Python 依赖
cd backend && pip install -r requirements.txt && cd ..

# 跑后端测试还需要测试依赖（CI 的 ubuntu job 装同一份）
cd backend && pip install -r requirements-dev.txt && cd ..

# 可选：web_fetch 工具增强（网页正文提取，未安装时自动降级）
cd backend && pip install -r requirements-optional.txt && cd ..

# 开发模式启动
npm run dev
```

### 命令

```bash
npm run dev            # 开发模式（Vite HMR + Electron）
npm run dev:web        # 开发模式 + dev server 监听 0.0.0.0（局域网真机/多屏调试）
npm run dev:backend    # 单独启动 Python 后端
npm run build          # 构建生产版本（out/main|preload|renderer）
npm run typecheck      # 前端类型检查（vue-tsc，web + node/electron 两份 tsconfig 都要过）
npm test               # 前端单测（vitest）
npm run test:backend   # 后端单测（pytest）
npm run ci             # 本地门禁：转调 scripts/ci-check.sh（同一份清单，含构建）
bash scripts/ci-check.sh   # 同上，直接跑（与 .github/workflows/ci.yml 检查项一致）
npm run package        # electron-builder 打包（Windows nsis + portable）
```

### 已知命名债：`backend/tests/test_*_roundN.py`

71 个后端测试模块里有 20 个沿用历史审查轮次编号（`round3` … `round20`，如
`test_minor_round3.py`、`test_post_process_round13b.py`、`test_settings_fields_round20.py`）。
编号对读代码的人没有信息量 —— 文件名说的其实是"这轮顺手补的回归"，主题才是被测面。

本轮**决定不改名**：改名会让 `git log -M` 追不到这些模块的演进史，而它们是历次
审查的回归锚点（多条断言只有结合当轮缺陷才讲得通）。新测试请按主题命名
（如 `test_safety_misjudgment.py`），别续上 `round21`。

### 打包与分发（最后一公里）

- **解释器解析顺序**（`electron/backend-manager.ts`）：`CKUN_PYTHON` 环境变量 →
  打包内 `resources/python-runtime/python.exe` → 系统 `python`/`python3`。
- **内嵌 Python 运行时**：将 portable/embeddable Python 准备到仓库根 `runtime/`
  （步骤见 `runtime/README.md`），`npm run package` 自动随包分发；未准备时构建
  打警告并回退系统 Python（目标机需自装）。
- **图标**：`resources/icon.png`（buildResources），electron-builder 打包时自动
  生成各尺寸 .ico /.icns 资源。
- **代码签名**：未配置证书（win 无 `certificateFile`），electron-builder 跳过
  签名并打警告 — 内网分发场景预期行为， SmartScreen 需自行处理。
- **CI**：仓库托管在 Gitee，`.github/workflows/ci.yml` 仅在推 GitHub 时生效；
  Gitee 侧以 `bash scripts/ci-check.sh`（等价 `npm run ci`）作为等效门禁。

## License

[MIT](LICENSE)
