# cKunAgentSSH

SSH 智能运维终端，将连接管理、远程 / 本地终端、SFTP、Docker、Git、实时监控、定时任务、SSH 隧道、运维审计与 AI Agent 集中在一个 IDE 风格的桌面工作区。AI 可沿同一套主机连接执行运维操作，并受审批与审计约束。

**前端**：Electron + Vue 3 + TypeScript　**AI 后端**：Python FastAPI　**发布目标**：Windows x64

[功能](#功能) · [技术栈](#技术栈) · [架构](#架构) · [快速开始](#快速开始) · [打包与发布](#打包与发布) · [日志与排障](#日志与排障)

## 功能

标题栏提供 9 个功能入口与设置页，作为应用全部能力的统一入口。

| 入口 | 页面 | 快捷键 | 说明 |
| --- | --- | --- | --- |
| AI | 首页任务台 | `Ctrl+Shift+B` | Agent 运行、审批与轨迹；徽章提示待审批 / 运行中 / 未读 |
| Docker | 远程 Docker | `Ctrl+Shift+6` | 容器、镜像、卷、网络 |
| Git | Git 工具 | `Ctrl+Shift+7` | GitHub 加速与仓库管理 |
| SFTP | 文件传输 | `Ctrl+Shift+2` | 双栏文件管理与断点续传 |
| 终端 | 远程管理 | `Ctrl+Shift+1` | 远程 / 本地多标签终端 |
| 监控 | 监控仪表盘 | `Ctrl+Shift+3` | 主机与 GPU 指标 |
| 定时 | 定时任务 | `Ctrl+Shift+4` | 计划任务与失败诊断 |
| 隧道 | SSH 隧道 | `Ctrl+Shift+5` | TCP / SOCKS5 / Dynamic 转发 |
| 轨迹 | 运维轨迹 | `Ctrl+Shift+8` | 变更与 Agent 运行记录 |
| 设置 | 设置 | `Ctrl+,` | 模型、记忆、主题、权限 |

### AI 能力

应用内没有独立的「聊天模式」。任务台的对话与执行统一进入 Agent 运行端点（`POST /agent/run`，SSE），由后端完成意图判定、上下文装配、工具调用、审批与审计；结构化、提交信息、命令预测等辅助能力走各自的无状态轻量端点。

**任务台与运行过程**

- 任务台是 AI 的唯一对话界面：输入区承载目标主机、目标 / 计划姿态、技能、文本附件与模型选择；运行中发送键变为停止键，草稿保留。
- 顶栏 AI 图标与 `Ctrl+Shift+B` 可在任意页面往返任务台；徽章按「待审批 > 运行中 > 未读」分级提示。
- 执行过程不刷屏：审批与澄清集中在输入框上方；常规运行状态与工具调用明细不在消息流展开，统一由运维轨迹页按 `run_id` 回放。消息流末尾的运行通知区只保留任务规划、警告、人工求助与错误。
- 支持思考链展示、Markdown 流式渲染、复制与会话历史；长会话分批渲染，避免一次性挂载全部消息。
- SSE 心跳与停顿检测并行：后端每 15 秒发送心跳，连续 90 秒收不到字节即判定连接停滞，不会把半截响应当作完成。

**Harness 与执行循环**

- ReAct 决策循环由 Harness 驱动，拦截器链覆盖技能注入、文件系统保护、循环检测、权限门、审计、动态步数预算、成本与墙钟熔断、置信度、反思与复盘、澄清、计划纪律。
- 步数预算按任务复杂度动态分配；临近上限时提示收敛，最后一次迭代物理禁用工具，避免「假完成」；结束前执行完成度校验与目标自检。
- 运行前只读探测目标机发行版、init、包管理器与登录身份，写入主机画像；按变更 / 只读任务注入对应的操作纪律。
- 运行结束或失败后触发反思；策略引擎依据近期完成度、效率、鲁棒性与成本演化迭代上限、温度与思考开关，并设边界防止震荡。后台心跳每 5 分钟对账记忆、修剪衰减，并将重复失败模式沉淀为经验候选。

**工具与远程执行**

- 34 个内置工具，覆盖 Shell 执行、联网检索、待办、技能加载、子 Agent、主机与进程观测、多会话巡检、服务管理、定时任务、日志读取与清理、远程文件读写编辑检索、图片查看、后台任务、记忆自编辑、Runbook 经验库与 Spec/Plan/Goal 文档落盘。
- 工具按风险等级（安全 / 中等 / 危险）标注，可在设置中按风险组启停。
- Agent 的远程操作经 WebSocket 反向通道 `/ws/bridge` 回到 Electron 主进程，再由 ssh2 执行；通道断开时 fail-closed，在途请求不重发。
- 权限与交付分离：权限卡决定「能不能执行」，交付卡决定「是否写入可见终端」，两者都只在审批坞出现。

**记忆系统**

- 混合检索：SQLite FTS5 的 BM25 与向量 KNN 经 RRF 融合；写入时按语义相似度去重，召回受注入预算约束。
- 常驻画像块保存用户偏好、Agent 规则与逐主机画像；主机画像来自只读探测并定期刷新。
- 记忆写入走后台任务队列，由 Reconciler 统一裁决新增、更新、删除或忽略；支持审计、历史回滚与长期未命中衰减。
- 设置页可浏览、检索、置顶、编辑、失效记忆，查看对账任务与画像块历史；排障经验可以 Runbook 形式沉淀并检索。

**权限与审批**

- 审批模式分「危险操作逐条确认」与「模型代判，拿不准仍弹卡」；审批坞按澄清、权限、终端交付的顺序排队。低风险且非生产环境的权限批次可一键全部批准，危险操作、生产环境与风险未知项必须逐条确认。
- 审批卡展示风险等级、目标主机、环境、完整命令、中文后果说明（模型生成时标注仅供参考）与写入 diff；操作分为允许本次、本会话允许、始终允许、拒绝。危险操作不提供「本会话允许」，只能单次授权或永久登记；显式拒绝在该轮运行内优先于任何缓存。
- 授权按主机限域：在测试机登记的命令不会自动在生产机免审。永久授权支持 TTL；「允许本次」严格绑定当前 `run_id`，不会泄漏给后续运行。
- 生产环境的危险操作只认预先登记的 allowlist，临时批准不生效；审批超时后待办保留且不会静默放行，批准后 AI 仍需重新发起同一命令。
- 结构化为任务补入写步骤时，自动审批会回退为逐条确认。

**姿态、澄清与结构化**

- 目标与计划两种姿态以 chip 形式显形。计划姿态成对下发 `workflow_mode=plan` 与 `permission_mode=plan`，写操作在审批之前即被硬性拦截为只读。
- 意图不足时弹出多步澄清卡片，支持选项、自定义输入与跳过，避免带着错误假设执行。
- 长指令可在发送前做一次结构化：不使用工具，只将原文拆解为明确步骤并补全运维字段，标注原文、推断、补全与新增内容；草稿绑定当前目标主机，计划姿态会剔除写步骤。采用与回退都由用户显式裁决，结构化失败不影响原草稿发送。

**模型管理**

- 支持本地 vLLM 与 OpenAI 兼容端点：DeepSeek、OpenAI、Qwen、Kimi、GLM、Xiaomi、Anthropic。
- 可测试连通性与延迟，分别设置全局默认与 SSH 场景默认；对话内可临时切换模型。缺少默认模型时后端会明确拒绝运行，而不是静默换用其他模型。
- 模型可配置上下文与输出上限、思考预算，并标记思考、视觉、工具调用能力；端点拒绝工具调用时自动降级为无工具模式。

**后处理与技能**

- 单轮对话结束后可自动生成会话标题、提取记忆、沉淀技能；技能生成每 3 轮触发一次且有数量上限，全部可在设置中关闭。
- 支持按触发词自动匹配已启用技能；内置 `linux-ops-triage`、`nginx-service-diagnose`、`ops-change-safety` 三套运维技能。

**跨页面 AI 入口**

- **终端选区**：选中内容后点击悬浮按钮，将选区连同工作目录与最近命令预填到任务台。
- **批量命令**：多机执行前调用安全分类，判定失败按危险处理；结果并排对比后可送入任务台分析。
- **定时任务**：失败记录可一键预填 AI 诊断；巡检任务按约定输出 `[巡检结论]`，异常时写入记忆并按配置推送 webhook。
- **Git**：按选中文件的 diff 生成提交信息，失败时回退规则草稿。
- **命令预测**：Tab 补全可接入模型预测，默认关闭，可在外观设置中开启。

**审计与轨迹**

- Agent 的每次工具调用、审批与授权写入 SHA-256 哈希链，支持完整性校验、CSV 导出与归档。
- 运维轨迹按 `run_id` 归集主机、命令、退出码、结论与完整执行链；消息上的「轨迹」可直接定位到对应记录。

### 终端与文件

- **SSH 连接管理**：支持密码、私钥与 SSH Agent 认证；凭据经系统 safeStorage 加密后持久化，配置可导入导出。
- **远程终端**：ssh2 + xterm.js，支持 OSC 7 工作目录跟踪与 LS_COLORS。
- **本地终端**：node-pty 驱动 PowerShell、CMD、Git Bash 与 WSL。
- **SFTP**：断点续传、分块并发、心跳检测、错误分类；传输中心展示进度与历史，常用远端目录可保存为书签。
- **远程文件编辑**：内置 Monaco Editor，可直接编辑远端文件。
- **命令辅助**：Tab 键远程补全、历史命令模糊联想与执行次数徽标、自定义快捷命令栏。
- **多节点批量下发**：向多台活动会话并行执行命令，结果并排对比并高亮行级差异；生产环境或危险命令在执行前强制二次确认。
- **安全接入**：除直连外，可经自营 TLS 网关中转，支持 SPKI 证书固定、口令认证与失败限速锁定；网关为单文件、零依赖实现。

### 观测与自动化

- **监控仪表盘**：每 5 秒采集 CPU、内存、负载、磁盘、进程 TOP、监听端口与 GPU；提供趋势图。连续失败后自动转入后台重试，连接恢复后继续。
- **GPU 面板**：nvitop 风格逐卡视图，展示算力、显存、温度、功耗与进程，并标注占卡不干活、IO 等待、SWAP 已满等异常状态。
- **定时任务**：倒计时、历史耗时与失败记录；可由 Agent 执行巡检。
- **SSH 隧道**：TCP 端口转发、SOCKS5 代理与 Dynamic 转发；规则持久化、状态机管理、端口冲突预检，隧道使用独立连接，不依赖终端会话。
- **Docker 管理**：通过远程 SSH 会话管理容器、镜像、卷与网络；支持容器详情、实时 stats、日志、镜像导出与加载、未用镜像清理与镜像拉取，危险操作二次确认并审计。
- **GitHub 加速**：镜像测速与收敛、hosts 精确写入与还原、自检与失效巡检。
- **Git 仓库管理**：双栏工作台，左侧按状态分组，右侧提供变更、分支与提交图谱；支持本地及远程仓库总览、批量获取、`pull --ff-only`、分支切换、按文件勾选提交、提交并推送、推送与打开终端；写操作确认后计入审计。

### 体验与工程

- **键盘优先**：命令面板（`Ctrl+Shift+P`）、页面直达（`Ctrl+Shift+1..8`）与终端标签快捷键。
- **外观体系**：9 档主题 × 10 套强调色，支持渐变模式与方向配置。
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
| 框架 | Python 3.11+ / FastAPI | Agent 运行、记忆与模型管理 |
| ASGI | uvicorn | 运行 FastAPI |
| Agent 编排 | 自研 Harness | ReAct 循环、拦截器链、审批与工具执行 |
| LLM 客户端 | openai SDK | OpenAI 兼容协议（vLLM / Ollama / OpenAI 等） |
| 存储 | SQLite | 会话、记忆（FTS5 + 向量）、技能与审计 |

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
   • Agent 运行（SSE，/agent/run）
   • Harness / 工具 / 审批
   • 记忆与审计（SQLite）
   • 模型配置与路由
          │
          ▼
   OpenAI 兼容 LLM 服务
```

**三条数据流：**

- **AI 运行**：Renderer → HTTP / SSE → Python FastAPI；对话与执行统一走 `/agent/run`，结构化、提交信息、命令预测等辅助能力走专用端点。
- **终端 / SSH / SFTP / 隧道**：Renderer → Electron IPC → node-pty / ssh2，不经过 Python。
- **Agent 远程执行**：Python 后端 → WebSocket 反向通道 `/ws/bridge` → Electron 主进程 → ssh2。通道断开时 fail-closed，在途请求不重发。

## 项目结构

```
cKunAgentSSH/
├── electron/              # Electron 主进程：窗口、IPC、SSH/SFTP/PTY/隧道、后端与网关管理
├── src/                   # Vue 3 渲染进程：页面、组件、composables、Pinia store、API
├── shared/                # 主进程 / 渲染进程 / 网关共享的纯逻辑与协议
├── backend/               # Python FastAPI：Agent、记忆、工具、路由与测试
│   ├── agent/             # Harness、记忆、工具、意图识别、反思演化、桥接转发
│   ├── routes/            # agent / chat（附件与会话）/ schedules / audit / settings / skills / terminal / workflow
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
- 可访问的 OpenAI 兼容 LLM 服务（vLLM / Ollama / OpenAI 等）

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

首次启动不预填任何模型，请在 **设置 → 模型** 中添加端点并指定默认模型；未指定时 Agent 运行会被明确拒绝。

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

- 图标：应用图标为 `resources/icon.png`；托盘优先使用 `resources/icon-tray.png`，缺失时回退整图。
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
