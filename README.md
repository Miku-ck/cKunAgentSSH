# cKunAgentSSH

SSH 智能运维终端。将连接管理、远程/本地终端、SFTP、Docker、Git、实时监控、定时任务、SSH 隧道、运维审计与 AI Agent 集中在一个 IDE 风格的桌面工作区。

**前端**：Electron + Vue 3 + TypeScript　**AI 能力**：Python FastAPI　**发布目标**：Windows x64

[功能](#功能) · [技术栈](#技术栈) · [架构](#架构) · [快速开始](#快速开始) · [打包与发布](#打包与发布) · [日志与排障](#日志与排障)

## 功能

标题栏提供 9 个功能入口与设置页，作为应用全部能力的统一入口。

| 入口 | 页面 | 快捷键 | 说明 |
| --- | --- | --- | --- |
| AI | 首页任务台 | `Ctrl+Shift+B` | AI 对话与执行过程；徽章提示待审批 / 运行中 / 未读 |
| Docker | 远程 Docker | `Ctrl+Shift+6` | 容器、镜像、卷、网络 |
| Git | Git 工具 | `Ctrl+Shift+7` | GitHub 加速与仓库管理 |
| SFTP | 文件传输 | `Ctrl+Shift+2` | 双栏文件管理与断点续传 |
| 终端 | 远程管理 | `Ctrl+Shift+1` | 远程 / 本地多标签终端 |
| 监控 | 监控仪表盘 | `Ctrl+Shift+3` | 主机与 GPU 指标 |
| 定时 | 定时任务 | `Ctrl+Shift+4` | 计划任务与失败诊断 |
| 隧道 | SSH 隧道 | `Ctrl+Shift+5` | TCP / SOCKS5 / Dynamic 转发 |
| 轨迹 | 运维轨迹 | `Ctrl+Shift+8` | 变更与 Agent 运行记录 |
| 设置 | 设置 | `Ctrl+,` | 模型、记忆、主题、权限 |

### 终端与文件

- **SSH 连接管理**：支持密码、私钥与 SSH Agent 认证；凭据经系统 safeStorage 加密后持久化，配置可导入导出。
- **远程终端**：ssh2 + xterm.js，支持 OSC 7 工作目录跟踪与 LS_COLORS。
- **本地终端**：node-pty 驱动 PowerShell、CMD、Git Bash 与 WSL。
- **SFTP**：断点续传、分块并发、心跳检测、错误分类；传输中心展示进度与历史，常用远端目录可保存为书签。
- **远程文件编辑**：内置 Monaco Editor，可直接编辑远端文件。
- **命令辅助**：Tab 键远程补全、历史命令模糊联想与执行次数徽标、自定义快捷命令栏。
- **多节点批量下发**：向多台活动会话并行执行命令，结果并排对比并高亮行级差异，可一键送入 AI 分析；生产环境或危险命令在执行前强制二次确认，并写入审计链。
- **安全接入**：除直连外，可经自营 TLS 网关中转，支持 SPKI 证书固定、口令认证与失败限速锁定；网关为单文件、零依赖实现。

### 观测与自动化

- **监控仪表盘**：每 5 秒采集 CPU、内存、负载、磁盘、进程 TOP、监听端口与 GPU；提供趋势图，单项指标可送 AI 分析。连续失败后自动转入后台重试，连接恢复后继续。
- **GPU 面板**：nvitop 风格逐卡视图，展示算力、显存、温度、功耗与进程，并标注占卡不干活、IO 等待、SWAP 已满等异常状态。
- **定时任务**：倒计时、历史耗时与失败运行诊断。
- **SSH 隧道**：TCP 端口转发、SOCKS5 代理与 Dynamic 转发；规则持久化、状态机管理、端口冲突预检，隧道使用独立连接，不依赖终端会话。
- **Docker 管理**：通过远程 SSH 会话管理容器、镜像、卷与网络；支持容器详情、实时 stats、日志、镜像导出与加载、未用镜像清理与镜像拉取，危险操作二次确认并审计。
- **GitHub 加速**：镜像测速与收敛、hosts 精确写入与还原、自检与失效巡检。
- **Git 仓库管理**：双栏工作台，左侧按状态分组，右侧提供变更、分支与提交图谱；支持本地及远程仓库总览、批量获取、`pull --ff-only`、分支切换、按文件勾选提交、提交并推送、推送与打开终端；写操作确认后计入审计。
- **运维轨迹**：按 run 归集变更与 Agent 操作，记录主机、命令、退出码与结论，并支持从消息中的 `run_id` 直达对应记录。

### AI 助手

- **首页任务台**：AI 的唯一对话界面，与终端通过顶栏 AI 图标或 `Ctrl+Shift+B` 往返；运行通知挂载在消息流末尾。
- **终端问 AI**：选中终端内容后点击悬浮按钮，将选区连同工作目录与最近命令预填到任务台。
- **对话与模型**：流式响应、思考链、多模型切换与工具调用。
- **Agent Harness**：ReAct 决策循环、拦截器链、动态步数预算、循环检测与成本熔断。
- **工具集**：34 个内置工具，覆盖 shell 执行、联网、待办、技能加载、子 Agent、远程观测与服务、文件读写检索、图片查看、后台任务、记忆编辑与 Spec/Plan/Goal 工作流。
- **记忆系统**：混合检索（FTS5 + 向量）、常驻画像块、注入预算，以及去重、矛盾消解与失效对账；可在设置中浏览、检索、置顶、编辑与失效。
- **计划模式**：前端成对下发 `workflow_mode=plan` 与 `permission_mode=plan`，写操作在审批前即被硬性拦截为只读。
- **澄清与审批**：意图不明时弹出多步澄清卡片；生产环境仅接受预授权 allowlist，破坏性操作逐次审批，审批卡附带中文后果说明。
- **环境与方法论注入**：运行前探测目标机发行版、init、包管理器与身份；按变更 / 只读任务注入对应的操作纪律。

### 体验与工程

- **键盘优先**：命令面板（`Ctrl+Shift+P`）、页面直达（`Ctrl+Shift+1..8`）与终端标签快捷键。
- **外观体系**：9 档主题 × 10 套强调色，支持渐变模式与方向配置。
- **内置运维技能**：`linux-ops-triage`、`nginx-service-diagnose`、`ops-change-safety`。
- **CI 门禁**：后端 pytest、前端类型检查、前端单测、electron-vite 构建与网关字节漂移自检。

## 技术栈

### 桌面端

| 层 | 技术 | 用途 |
| --- | --- | --- |
| 桌面框架 | Electron 35 | 桌面应用壳 |
| 前端 | Vue 3 + Vite 6 | 渲染进程 SPA |
| 状态管理 | Pinia | 全局状态与会话管理 |
| 代码编辑器 | Monaco Editor | 远程文件编辑 |
| 终端 | xterm.js + node-pty | 本地终端 |
| SSH | ssh2 | 远程终端、SFTP、隧道 |
| 构建 | electron-vite | 主进程 / 预加载 / 渲染进程构建 |
| 打包 | electron-builder | Windows dir 产物与 tar.gz 分发 |

### 后端

| 层 | 技术 | 用途 |
| --- | --- | --- |
| 框架 | Python 3.11+ / FastAPI | AI 对话、Agent 与模型管理 |
| ASGI | uvicorn | 运行 FastAPI |
| LLM 客户端 | openai SDK | OpenAI 兼容协议（vLLM / Ollama / OpenAI） |
| 存储 | SQLite | 对话历史、会话状态、记忆与技能配置 |

## 架构

```
┌──────────────────────────── Electron ────────────────────────────┐
│  Renderer (Vue 3)                 Main (Node.js)                 │
│  任务台 / 终端 / 编辑器            node-pty / ssh2 / BackendManager │
│  xterm.js / Pinia                        ▲                       │
│         │ HTTP / SSE                     │ WS /ws/bridge          │
└─────────┼────────────────────────────────┼───────────────────────┘
          ▼                                │
   Python FastAPI 后端 ────────────────────┘
   • Chat 流式对话（SSE）
   • Agent Harness
   • 会话与记忆（SQLite）
   • 模型配置与路由
          │
          ▼
   OpenAI 兼容 LLM 服务
```

**三条数据流：**

- **AI 对话**：Renderer → HTTP / SSE → Python FastAPI。
- **终端 / SSH / SFTP / 隧道**：Renderer → Electron IPC → node-pty / ssh2，不经过 Python。
- **Agent 远程执行**：Python 后端 → WebSocket 反向通道 `/ws/bridge` → Electron 主进程 → ssh2。通道断开时 fail-closed，在途请求不重发。

## 项目结构

```
cKunAgentSSH/
├── electron/              # Electron 主进程：窗口、IPC、SSH/SFTP/PTY/隧道、后端与网关管理
├── src/                   # Vue 3 渲染进程：页面、组件、composables、Pinia store、API
├── shared/                # 主进程 / 渲染进程 / 网关共享的纯逻辑与协议
├── backend/               # Python FastAPI：对话、Agent Harness、记忆、工具、路由与测试
│   ├── agent/             # Agent 框架（harness / memory / tools / intention / bridge_relay）
│   ├── routes/            # chat / agent / schedules / audit / settings / skills / terminal / workflow
│   └── tests/             # pytest 测试
├── gateway/               # TLS 接入网关（单文件、零依赖、独立部署）
├── scripts/               # dev / package / clean / ci-check / rebuild-native / deploy
├── resources/             # 应用与托盘图标
├── docs/                  # 设计与实施 spec
├── electron.vite.config.ts
├── electron-builder.yml
└── package.json
```

## 快速开始

### 环境要求

- Node.js 20+
- Python 3.11+
- 可访问的 OpenAI 兼容 LLM 服务（vLLM / Ollama / OpenAI）

### 安装与启动

```bash
npm install
pip install -r backend/requirements.txt
npm run dev
```

后端测试依赖另行安装：

```bash
pip install -r backend/requirements-dev.txt
```

### 模型配置

首次启动不预填任何模型，请在 **设置 → 模型** 中添加 LLM 端点。

开发者或演示环境可在 `backend/.env` 中开启首启自动探测：

```bash
# 必填：首启探测
VLLM_BASE_URL=http://192.168.150.40:9030/v1
# 可选：思考模型
VLLM_THINKING_URL=http://192.168.150.40:9031/v1
# 可选：API Key
VLLM_API_KEY=EMPTY
```

### 常用命令

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 开发模式（Vite HMR + Electron） |
| `npm run dev:web` | 开发模式并监听 `0.0.0.0`（POSIX：`dev:web:unix`） |
| `npm run dev:backend` | 单独启动后端，默认监听 `0.0.0.0`；仅本机使用可追加 `-- --host 127.0.0.1` |
| `npm run build` | 构建生产版本 |
| `npm run typecheck` | 前端类型检查 |
| `npm test` | 前端单测（vitest） |
| `npm run test:backend` | 后端单测（pytest） |
| `npm run ci` | 本地完整门禁，与 CI 检查项一致 |
| `npm run package` | 打包 Windows 产物与 tar.gz |
| `npm run clean` | 清理打包产物与中间配置 |
| `npm run preview` | 预览构建结果 |

## 打包与发布

```bash
npm run package
```

打包流程：

1. `electron-vite build` 生成 `out/`。
2. electron-builder 生成解包目录 `dist/win-unpacked/`。
3. `scripts/package.js` 压缩为 `dist/ckunagentssh-<version>-win32-x64.tar.gz`。

发布形态为 **tar.gz 解包直发**，不提供安装器。桌面与开始菜单快捷方式由应用首次启动时创建；解包目录移动后，应用按 exe 指纹自动重建失效快捷方式。

**内嵌 Python 运行时**：`npm run package` 默认要求仓库根目录存在 `runtime/`（Windows 为 `runtime/python.exe`，POSIX 为 `runtime/bin/python3`），并将其打入 `resources/python-runtime/`。缺少运行时时打包会失败；仅验证流水线时可使用 `npm run package -- --allow-missing-runtime`。`runtime/` 已被 `.gitignore` 忽略，需本地或 CI 准备。

其他说明：

- 图标：`resources/icon.png`；托盘图标为 `resources/icon-tray.png`。
- 代码签名：当前未配置证书。

## 日志与排障

日志位于 Electron `userData` 目录：

| 文件 | 内容 | 格式与轮转 |
| --- | --- | --- |
| `main.log` | 主进程及转发的渲染层 console | `[HH:MM:SS.mmm] [LEVEL] 消息`；单文件 5 MB，保留 `.1` / `.2` 共 3 份 |
| `electron-bridge.log` | Bridge 通道与远程命令执行错误 | ISO 时间戳；单文件 1 MB，超限轮转为 `.old` |

- Python 后端日志格式为 `%(asctime)s %(levelname)-7s %(name)s: %(message)s`，时间格式 `%H:%M:%S`，默认级别 `warning`；uvicorn 访问日志已关闭，避免健康检查刷屏。
- Python 后端日志出口会脱敏口令等敏感字段。`userData` 不可写时，主进程落盘静默停用，不影响应用运行。

## 接入网关

网关用于内网自建中转，独立于 Electron 打包链：

```bash
node gateway/hillgw.mjs --config gw.json    # 启动
node gateway/hillgw.mjs --hash <口令>        # 生成配置中的 passwordSha256
node gateway/hillgw.mjs --selftest          # 帧编解码与策略匹配自检
```

服务端证书与私钥由运维提供；客户端通过 SPKI 指纹固定校验服务端身份。

## License

[MIT](LICENSE)
