# cKunAgentic

SSH 智能运维终端 — 集成 SSH 连接管理、远程终端、SFTP 文件传输与 AI 运维助手。深色 IDE 风格界面，统一字体（Segoe UI / SF Pro）与配色。

## 功能特性

- **SSH 连接管理**：密码 / 密钥 / Agent 认证，连接配置 safeStorage 加密持久化，导入/导出
- **远程终端**：ssh2 shell channel + xterm.js，支持 OSC 7 工作目录跟踪、LS_COLORS 优化
- **本地终端**：node-pty + xterm.js，支持 CMD / PowerShell / Git Bash / WSL
- **SFTP 文件传输**：断点续传（.part 临时文件）、分块并发、心跳检测、错误分类
- **远程文件编辑**：Monaco Editor 在线编辑远程文件
- **命令补全**：Tab 补全，通过 SSH exec 远程查询候选
- **快捷命令栏**：自定义命令快捷执行
- **AI 运维助手**：流式对话 + 思考链、多模型切换、Agent 智能体（工具调用）
- **Agent 框架（Harness）**：ReAct 决策循环、8 拦截器、14 工具、验证回路、动态步数预算、循环检测

## 技术栈

### 桌面端

| 层 | 技术 | 用途 |
|---|---|---|
| 桌面框架 | Electron 33+ | 跨平台应用壳（Win / macOS / Linux） |
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
        │     (uvicorn / bundled exe)    │
        │                                │
        │  • Chat 流式对话 (SSE)          │
        │  • Agent Harness 框架          │
        │    - ReAct 决策循环            │
        │    - 8 拦截器 (Reins)          │
        │    - 14 工具 (Saddle)          │
        │    - 验证回路 + 动态预算       │
        │  • 会话持久化 (SQLite)          │
        │  • 模型配置 / 路由             │
        │  • 技能系统 (SkillRegistry)    │
        └───────────────┬────────────────┘
                        │
                  vLLM (OpenAI 兼容)
```

**两条数据流：**

- **AI 对话**：Renderer → HTTP/SSE → Python FastAPI
- **终端 / SSH / SFTP**：Renderer → Electron IPC → node-pty / ssh2（纯 Node.js，不经 Python）

## 项目结构

```
cKunAgentSSH/
├── electron/                          # Electron 主进程
│   ├── main.ts                        # 入口，窗口管理，IPC handler 注册
│   ├── preload.ts                     # 安全桥接 API (contextBridge)
│   ├── backend-manager.ts             # Python 进程生命周期管理
│   ├── electron-bridge.ts             # 本地 HTTP 服务供后端调用主进程 SSH 能力
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
│   │       ├── QuickCommandBar.vue    # 快捷命令栏
│   │       ├── CompletionPopup.vue    # 命令补全弹窗
│   │       ├── NoteInput.vue          # 备注输入
│   │       └── QuickCommandEditor.vue # 快捷命令编辑
│   │
│   ├── composables/                   # 组合式函数
│   │   ├── useAgentIntegration.ts     # Agent 集成逻辑
│   │   ├── useAppearance.ts           # 主题/外观管理
│   │   ├── useChatPersistence.ts      # 聊天持久化
│   │   ├── useChatStream.ts           # 流式聊天
│   │   ├── useCompletion.ts           # 命令补全
│   │   ├── useConfirm.ts              # 全局确认对话框
│   │   ├── useModelSelector.ts        # 模型选择
│   │   ├── useResizablePanel.ts       # 可拖拽面板
│   │   ├── useSettings.ts             # 设置对话框
│   │   ├── useCommandDelivery.ts      # 命令交付/焦点管理
│   │   ├── useSshNavigation.ts        # SSH 导航与终端切换
│   │   ├── useTabContextMenu.ts       # 标签右键菜单
│   │   └── useTerminalConfig.ts       # 终端配置
│   │
│   ├── stores/                        # Pinia 状态
│   │   ├── app.ts                     # 后端状态管理
│   │   ├── ssh.ts                     # SSH 会话池 + 连接配置
│   │   ├── sftp.ts                    # SFTP 传输状态
│   │   ├── ssh-ai.ts                  # SSH AI 对话
│   │   ├── chat.ts                    # 通用对话管理
│   │   ├── agent.ts                   # Agent 运行状态
│   │   ├── skills.ts                  # 技能管理
│   │   ├── quickCommands.ts           # 快捷命令
│   │   ├── settings.ts                # 应用设置
│   │   ├── createModeChatStore.ts     # 创建模式会话存储
│   │   └── toast.ts                   # 消息提示
│   │
│   └── utils/
│       ├── id.ts                      # ID 生成
│       ├── language.ts                # 语言检测
│       ├── markdown.ts                # Markdown 渲染
│       ├── terminal-colors.ts         # ANSI 背景色过滤
│       └── think-scrubber.ts          # 思考链清理
│
├── backend/                           # Python FastAPI 后端
│   ├── main.py                        # FastAPI 入口 + 路由注册
│   ├── config.py                      # 配置管理
│   ├── database.py                    # SQLite 操作层
│   ├── llm_adapter.py                 # OpenAI 兼容流式客户端
│   ├── prompts.py                     # 系统提示词构建
│   ├── post_processor.py              # 后处理（记忆/技能/标题生成）
│   ├── skill_registry.py              # 技能注册表
│   ├── safe_tokens.py                 # Token 安全处理
│   ├── sse_utils.py                   # SSE 工具函数
│   ├── requirements.txt               # Python 依赖
│   │
│   ├── agent/                         # Agent 智能体框架
│   │   ├── harness/                   # Harness 核心（马术装备隐喻）
│   │   │   ├── harness.py             # 组装入口
│   │   │   ├── driver.py              # ReAct 决策循环主驱动
│   │   │   ├── bridle.py              # 上下文组装 + 压缩
│   │   │   ├── saddle.py              # 工具执行 + 沙箱
│   │   │   ├── stirrups.py            # LLM 调用 + 重试
│   │   │   ├── reins.py               # 8 拦截器策略层
│   │   │   ├── safety.py              # 命令/路径/SSRF 安全防御
│   │   │   ├── telemetry.py           # 遥测总线
│   │   │   ├── contracts.py           # 契约定义
│   │   │   └── llm_client.py          # 流式 LLM 客户端
│   │   │
│   │   ├── memory.py                  # Agent 记忆管理
│   │   ├── reflection.py              # 元反思
│   │   ├── evolution.py               # 策略演化
│   │   ├── intention.py               # 意图分类
│   │   ├── audit.py                   # 审计引擎（链式签名）
│   │   ├── cost_tracker.py            # 成本追踪
│   │   ├── file_changes.py            # 文件变更追踪
│   │   ├── consciousness_tick.py      # 后台心智维护
│   │   ├── electron_bridge.py         # 桥接主进程 SSH exec
│   │   │
│   │   └── tools/builtin/             # 14 个内置工具
│   │       ├── shell_exec.py          # SSH 远程命令执行
│   │       ├── file_ops.py            # 文件读写操作
│   │       ├── patch_file.py          # 精确替换
│   │       ├── grep_tool.py           # 内容搜索
│   │       ├── glob_tool.py           # 文件查找
│   │       ├── web_tools.py           # 联网搜索/抓取
│   │       ├── todo_write.py          # 任务列表
│   │       ├── load_skill.py          # 按需加载技能
│   │       └── spawn_subagent.py      # 启动子 Agent
│   │
│   └── routes/
│       ├── chat.py                    # 对话接口（SSE 流式）
│       ├── agent.py                   # Agent 运行接口（SSE + 工具调用）
│       ├── models.py                  # 模型配置查询
│       ├── settings.py                # 设置接口
│       ├── skills.py                  # 技能 CRUD
│       ├── terminal.py                # 终端信息接口
│       └── audit.py                   # 审计记录查询/导出/校验
│
├── electron.vite.config.ts            # electron-vite 构建配置
├── electron-builder.yml               # 打包配置
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
- **electron-bridge.ts** — 本地 HTTP 服务（127.0.0.1:30000），供 Python 后端调用主进程 SSH exec

### 渲染进程

- **App.vue** — 根布局：TitleBar + SshView，直接渲染 SSH 界面
- **TitleBar.vue** — 自定义标题栏：品牌标识 + 窗口控制按钮
- **SshView.vue** — SSH 主视图：Quick Start 首页 / 远程管理 / 本地终端 / SFTP / AI 面板 / 快捷命令栏

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
  ├── Reins（缰绳）—— 8 拦截器策略层
  │   ├── SkillRein         AGENT_START    技能注入
  │   ├── LoopDetector      PRE_TOOL_CALL  循环检测
  │   ├── PermissionGate    PRE_TOOL_CALL  权限审批
  │   ├── FilesystemGuard   PRE_TOOL_CALL  路径越界+敏感命令
  │   ├── SyntaxVerifier    POST_TOOL_CALL 语法验证
  │   ├── AuditLogger       POST_TOOL_CALL 审计日志
  │   ├── DynamicBudgetRein POST_ITERATION 动态步数预算
  │   └── ConfidenceMonitor POST_ITERATION 置信度监控
  │
  ├── Bridle（马缰）—— 上下文组装 + 压缩
  ├── Saddle（马鞍）—— 工具执行 + 沙箱（14 个内置工具）
  ├── Stirrups（马镫）—— LLM 调用 + 重试
  └── TelemetryBus —— 事件广播
```

### ReAct 决策循环

```
INIT → THINKING → [LLM 调用] → 有工具调用?
  ├─ 是 → ACTING → [工具执行] → OBSERVING → THINKING（循环）
  └─ 否 → 验证回路检查
         ├─ 有语法错误 → 注入错误 → THINKING（强制修复）
         └─ 无错误 → COMPLETED
达到 max_iterations → MAX_REACHED
```

### 14 个内置工具

| 工具 | 风险 | 说明 |
|---|---|---|
| `read_file` | safe | 读取文件（支持行范围，大文件截断） |
| `write_file` | moderate | 写入文件（覆盖） |
| `patch_file` | moderate | 精确替换（old_string → new_string） |
| `delete_file` | moderate | 删除文件 |
| `move_file` | moderate | 移动/重命名文件 |
| `list_dir` | safe | 列出目录内容 |
| `grep` | safe | 正则内容搜索 |
| `glob` | safe | 递归文件查找 |
| `shell_exec` | moderate | SSH 远程命令执行（命令级审批） |
| `web_search` | safe | 联网搜索（Tavily API） |
| `web_fetch` | safe | 抓取网页（SSRF 防护） |
| `todo_write` | safe | 任务列表管理 |
| `load_skill` | safe | 按需加载技能完整指令 |
| `spawn_subagent` | safe | 启动子 Agent 处理独立子任务（纯推理） |

### 安全防御

- **命令安全**：6 步归一化 + 12 HARDLINE 阻断 + 20 DANGEROUS 审批
- **路径安全**：realpath + commonpath 严格边界检查（防 symlink 逃逸）
- **SSRF 防护**：IP 拒绝私网 + DNS rebinding 防护 + 重定向校验

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

# 开发模式启动
npm run dev
```

### 命令

```bash
npm run dev          # 开发模式（Vite HMR + Electron）
npm run dev:backend  # 单独启动 Python 后端
npm run build        # 构建生产版本
```

## License

[MIT](LICENSE)
