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
- **GPU nvtop 面板**：nvitop 风格逐卡视图（算力/显存/温度/功耗/进程），双层量值图（进度条=当前值、趋势薄条=60 格历史），矛盾态检测（占卡不干活 / IO 等待 / SWAP 已满）以条端标记+悬浮提示呈现
- **终端「问 AI」**：选中终端内容 → 选区旁悬浮按钮 → 预填 AI 面板问题草稿，随消息自动注入 cwd / 最近命令 / 选区上下文
- **AI 运维助手**：流式对话 + 思考链、多模型切换、Agent 智能体（工具调用）
- **Agent 框架（Harness）**：ReAct 决策循环、13 拦截器、29 工具、动态步数预算、循环检测、成本熔断
- **Agent 记忆 v2**：单一写入方对账（去重/消解矛盾/失效）、混合检索（FTS5 + 向量）、常驻画像块、注入 token 预算；设置 → 记忆 可浏览/检索/置顶/编辑/失效记忆并查看对账队列与块历史
- **计划模式**：前端 mode chip 成对下发 `workflow_mode=plan` + `permission_mode=plan`，后者由 `PlanModeGuard` 在审批卡之前就把写操作硬拦成只读
- **澄清向导**：Agent 工具调用意图不明时拦截并弹出多步澄清卡片（A/B/C + 自定义），答完注入上下文放行、可跳过；精准调用零打扰
- **生产环境授权强化**：prod 环境仅认预授权 allowlist（禁临时审批），service stop/restart 等破坏性动作自动升级 dangerous 需逐次审批
- **审批卡片中文说明**：需审批命令自动附中文后果说明（静态后果表全覆盖），未知命令由 LLM 后台补发一句解释回填卡片
- **审批焦点队列**：并发审批请求按最旧优先单卡聚焦、+N 折叠可逐条切换，支持全部允许/全部拒绝；待办常驻（超时可配置，默认 600s 兜底），同命令重发复用待办不重复建卡
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
- **Agent 远程执行**：Python 后端 → WS 反向通道（`/ws/bridge`，主进程出站拨号、长期保持）→ Electron 主进程 dispatch → ssh2。断线 fail-closed，在途请求绝不重发

## 项目结构

```
cKunAgentSSH/
├── electron/                          # Electron 主进程
│   ├── main.ts                        # 入口，窗口管理，IPC handler 注册
│   ├── preload.ts                     # 安全桥接 API (contextBridge)
│   ├── backend-manager.ts             # Python 进程生命周期管理
│   ├── electron-bridge.ts             # 桥接业务实现（token 生成 + dispatch）
│   ├── pty-manager.ts                 # node-pty 终端实例池
│   ├── ssh-manager.ts                 # ssh2 连接池 + SFTP
│   ├── ssh-store.ts                   # 连接配置持久化存储（safeStorage 加密）
│   └── utils/                         # 工具模块
│
├── src/                               # Vue 3 渲染进程
│   ├── App.vue                        # 根组件
│   ├── main.ts                        # Vue 入口
│   ├── api/                           # HTTP / SSE 客户端
│   ├── layouts/                       # 布局组件
│   ├── views/                         # 页面视图
│   ├── components/                    # UI 组件
│   │   ├── common/                    # 通用组件
│   │   ├── agent/                     # Agent 组件
│   │   ├── chat/                      # 聊天组件
│   │   ├── icons/                     # SVG 图标
│   │   ├── settings/                  # 设置面板
│   │   └── ssh/                       # SSH 核心组件
│   ├── composables/                   # 组合式函数
│   ├── stores/                        # Pinia 状态管理
│   └── utils/                         # 工具函数
│
├── backend/                           # Python FastAPI 后端
│   ├── main.py                        # FastAPI 入口
│   ├── config.py                      # 配置管理
│   ├── database.py                    # SQLite 操作层
│   ├── llm_adapter.py                 # OpenAI 兼容流式客户端
│   ├── prompts.py                     # 系统提示词
│   ├── post_processor.py              # 后处理
│   ├── skill_registry.py              # 技能注册表
│   ├── builtin_skills.py              # 内置技能同步
│   ├── skills_builtin/                # 内置运维技能
│   ├── routes/                        # API 路由
│   └── agent/                         # Agent 智能体框架
│       ├── harness/                   # Harness 核心
│       ├── memory/                    # 记忆子系统
│       └── tools/builtin/             # 29 个内置工具
│
├── scripts/                           # 脚本
├── docs/                              # 文档
└── package.json
```

## 核心模块说明

### Electron 主进程

- **main.ts** — 入口。窗口创建（无边框 + 自定义标题栏）、IPC handler 注册
- **preload.ts** — `contextBridge` 暴露受限 API
- **backend-manager.ts** — Python 进程生命周期：spawn、健康检查、自动重启
- **pty-manager.ts** — node-pty 实例池，跨平台 shell 检测
- **ssh-manager.ts** — ssh2 连接池，支持密码/密钥认证、SFTP 断点续传
- **ssh-store.ts** — 连接配置持久化，safeStorage OS 级加密
- **electron-bridge.ts** — 桥接业务实现：WS 通道 req 帧的唯一业务实现

### 渲染进程

- **SshView.vue** — SSH 主视图：首页（目标主机 + 指令卡 + 待放行队列）/ 远程管理 / 本地终端 / SFTP / AI 面板 / 监控仪表盘
- **stores/** — Pinia 状态管理（ssh、sftp、ssh-ai、agent、settings 等）

### Python 后端

- **main.py** — FastAPI 入口，CORS 配置，路由注册
- **database.py** — SQLite 操作层
- **llm_adapter.py** — OpenAI 兼容流式客户端
- **routes/** — API 路由（chat、agent、settings、skills 等）

### Agent 框架

采用马术装备隐喻设计，包含：

- **Driver** — ReAct 决策循环主驱动
- **Reins** — 13 个拦截器（技能注入、循环检测、权限审批、文件系统守护、澄清向导、计划模式等）
- **Bridle** — 上下文组装 + 压缩
- **Saddle** — 工具执行 + 沙箱（29 个内置工具）
- **Stirrups** — LLM 调用 + 重试

## 开发

### 前置条件

- Node.js 20+
- Python 3.11+
- 可访问的 OpenAI 兼容 LLM 服务（vLLM / Ollama / OpenAI）

### 首次启动的模型配置

首次启动**不会预填任何模型**，请在 设置 → 模型 中添加你的 LLM 端点。

开发者/演示环境可在 `backend/.env` 中显式开启首启自动探测：

```bash
# 必填：首启探测
VLLM_BASE_URL=http://192.168.150.40:9030/v1
# 可选：思考模型
VLLM_THINKING_URL=http://192.168.150.40:9031/v1
# 可选：API Key
VLLM_API_KEY=EMPTY
```

### 启动

```bash
# 安装前端依赖
npm install

# 安装 Python 依赖
cd backend && pip install -r requirements.txt && cd ..

# 安装测试依赖
cd backend && pip install -r requirements-dev.txt && cd ..

# 开发模式启动
npm run dev
```

### 命令

```bash
npm run dev            # 开发模式（Vite HMR + Electron）
npm run dev:web        # 开发模式 + dev server 监听 0.0.0.0
npm run dev:backend    # 单独启动 Python 后端
npm run build          # 构建生产版本
npm run typecheck      # 前端类型检查
npm test               # 前端单测（vitest）
npm run test:backend   # 后端单测（pytest）
npm run ci             # 本地门禁
npm run package        # electron-builder 打包
```

### 打包与分发

- **解释器解析顺序**：`CKUN_PYTHON` 环境变量 → 打包内 `resources/python-runtime/python.exe` → 系统 `python`
- **内嵌 Python 运行时**：将 portable Python 准备到 `runtime/` 目录
- **图标**：`resources/icon.png`
- **代码签名**：未配置证书

## License

[MIT](LICENSE)