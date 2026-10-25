# cKunAgentSSH

SSH 智能运维终端 —— 一个窗口收口远程运维全链路：SSH 连接管理、多标签终端（远程 + 本地）、SFTP、Docker、Git（加速 + 仓库管理）、实时监控、定时任务、SSH 隧道、运维轨迹，以及贯穿其中的 AI Agent 助手。IDE 风格界面，9 档主题 × 10 套强调色。

## 功能地图（顶部导航 = 唯一入口）

标题栏右侧的九枚功能页入口 + 一枚设置，覆盖应用全部能力。它们是同一份页面清单 `useSshNavigation.NavPage` 的渲染结果：图标高亮、`Ctrl+Shift+数字` 快捷键、命令面板条目共用这一个来源，不存在手抄的第二份清单（加一页只改一处）。

| 入口 | 页面 | 快捷键 | 一句话职责 |
|---|---|---|---|
| **AI** | 首页任务台 | `Ctrl+Shift+B` | 助手唯一界面；徽章按 待审批 > 运行中 > 未读 点亮 |
| **Docker** | 远程 Docker 管理 | `Ctrl+Shift+6` | 容器 / 镜像 / 卷 / 网络四页签 |
| **Git** | Git（GitHub 加速 / 仓库管理） | `Ctrl+Shift+7` | 加速：镜像测速收敛 + hosts 精确还原；仓库：本地 + 远程状态总览 |
| **SFTP** | SFTP 文件传输 | `Ctrl+Shift+2` | 双栏文件管理 + 断点续传 |
| **终端** | 终端 / 远程管理 | — | 远程与本地多标签终端；零会话时落到远程管理页 |
| **监控** | 监控仪表盘 | `Ctrl+Shift+3` | 5s 轮询主机与 GPU 指标 |
| **定时** | 定时任务 | `Ctrl+Shift+4` | 倒计时与失败 run 诊断 |
| **隧道** | SSH 隧道 | `Ctrl+Shift+5` | TCP / SOCKS5 / Dynamic 转发 |
| **轨迹** | 运维轨迹 | `Ctrl+Shift+8` | 变更与 Agent 运行记录归案 |
| **⚙** | 设置 | `Ctrl+,` | 模型 / 记忆 / 主题 / 权限 |

布局与交互约束：

- 三组语义分区，组间以分隔线切分：**常用工具**（AI / Docker / Git / SFTP / 终端）紧贴品牌区，**会话与网络**（监控 / 定时 / 隧道）居中，**追溯 + 设置**（轨迹 / ⚙）靠近窗口控制。
- 宽度 ≥1024px 时常显 2 字标签（可发现性优先），以下收纳为纯图标；`title` 里的快捷键提示与 `PAGE_SHORTCUTS` 同源。
- 独立页与首页任务台互斥：任务台是盖在终端之上的浮层，切换只改 `showHome` / `activeNavPage` 两个标志。终端实例永不卸载 —— xterm 一卸载 buffer 就没了，所以「看不见」只能靠标志表达。
- 远程管理页已并入「终端」入口，仍可用 `Ctrl+Shift+1` 或命令面板「打开：远程管理」直达，只是标题栏不再单列按钮。

## 能力清单

### 终端与文件

- **SSH 连接管理**：密码 / 密钥 / Agent 认证，连接配置 safeStorage 加密持久化，支持导入导出
- **远程终端**：ssh2 shell channel + xterm.js，OSC 7 工作目录跟踪、LS_COLORS 优化
- **本地终端**：node-pty + xterm.js，PowerShell / CMD / Git Bash / WSL，启动即退出时给出针对性排查提示
- **SFTP**：断点续传（`.part` 临时文件）、分块并发、心跳检测、错误分类；传输中心看进度与历史，常用远端目录存为路径书签
- **远程文件编辑**：Monaco Editor 在线编辑远端文件
- **命令补全与快捷命令**：Tab 经 SSH exec 远程查候选；历史命令 fuzzy 联想（字符子序列 + 词首/连续加分）+ 执行次数徽标；自定义快捷命令栏
- **多节点批量下发**：对多台活动远程会话并行下发一条命令，结果并排对比 + 逐行差异高亮，可一键送 AI 分析；生产环境标记 / 危险命令命中时执行前强制二次确认，逐台写入审计链
- **安全接入**：直连之外可经自营网关 `gateway/hillgw.mjs` 中转（TLS + SPKI pinning + 口令认证 + 帧复用），网关零依赖单文件，`node gateway/hillgw.mjs --selftest` 自校验帧编解码与策略匹配

### 观测与自动化

- **实时监控仪表盘**：每 5s 轮询 CPU（跨轮次差分）/ 内存 / 负载 / 磁盘 / 进程 TOP / 监听端口 / GPU，Sparkline 趋势，单点指标可「分析」送 AI；连续失败转后台重试，断线自动恢复
- **GPU nvtop 面板**：nvitop 风格逐卡视图（算力 / 显存 / 温度 / 功耗 / 进程），双层量值图（进度条=当前值，趋势薄条=60 格历史），矛盾态（占卡不干活 / IO 等待 / SWAP 已满）以条端标记 + 悬浮提示呈现
- **定时任务**：倒计时、历史耗时、失败 run 一键「AI 诊断」
- **SSH 隧道**：TCP 端口转发 / SOCKS5 代理 / Dynamic 转发，规则持久化 + 状态机 + 端口冲突预检，独立 client 不依赖终端会话
- **Docker 管理**：依附远程 SSH 会话的容器 / 镜像 / 卷 / 网络四页签，按名称过滤，危险操作（如卷清理）二次确认并写入审计链，兼容老版本 Docker CLI；容器详情三段视图（配置 / 日志 / 实时 stats）、镜像导出 tar 与加载、未用镜像清理、pull
- **GitHub 加速**：一键被动加速（总控纳入 hosts）、转换结果按测速收敛、自检与失效巡检、hosts 文件字节保真与精确还原
- **Git 仓库管理**：双栏工作台——左侧状态分组列表（需处理 / 待拉取 / 待推送 / 干净，↑↓ 键盘导航），右侧常驻详情面板（变更 / 分支 / 图谱三页签）；本地 + 远程（SSH 会话）仓库状态总览（未提交变更 / 已分叉 / 落后，异常优先排序），获取全部（批量 fetch --all --prune）、fetch / pull --ff-only（远程 pull 即部署语义，计入审计），分支切换（git switch）、按文件勾选提交（add + commit -- pathspec，全选 / 清空，文件行带类型图标 + 状态短记）、一键提交并推送（一次确认覆盖两步）、推送（push，无上游 -u 建跟踪；远程写操作确认 + 审计），打开终端直落仓库目录，GitHub 远程与全局加速联动标记，提交图谱为 SVG 车道图（父提交推导车道，引用徽章 + 分页加载，失败回退平铺列表）
- **运维轨迹**：变更动作与 Agent 运行记录归案（主机 / 命令 / 退出码 / 结论），轮转保留；消息中的 run_id 可直达对应分段

### AI 助手

- **首页任务台**：助手的唯一界面（原右侧可拖宽面板已于 2026-10-20 退役）。无终端会话时独占全屏，有会话时与终端标签条经顶栏 AI 图标 / `Ctrl+Shift+B` 一键往返；运行过程通知（任务规划 / 警告 / 人工介入 / 错误）挂在消息流末尾
- **终端「问 AI」**：选中终端内容 → 选区旁悬浮按钮 → 预填任务台草稿（一次性 `pendingPrefill`，消费即清空），随消息自动注入 cwd / 最近命令 / 选区上下文
- **对话与模型**：流式对话 + 思考链、多模型切换、Agent 智能体（工具调用）
- **Harness 框架**：ReAct 决策循环、Reins 拦截器链、动态步数预算、循环检测、成本熔断
- **工具集**：34 个内置工具 —— shell 执行 / 联网 / 待办 / 技能按需加载 / 子 Agent / 运维观测与服务 / 远程文件读写检索 / 图片查看 / 后台任务 / 记忆自编辑 / Spec-Plan-Goal 落盘
- **记忆 v2**：单一写入方对账（去重 / 消解矛盾 / 失效）、混合检索（FTS5 + 向量）、常驻画像块、注入 token 预算；设置 → 记忆 可浏览、检索、置顶、编辑、失效，并查看对账队列与块历史
- **计划模式**：前端 mode chip 成对下发 `workflow_mode=plan` + `permission_mode=plan`，后者由 `PlanModeGuard` 在审批卡之前就把写操作硬拦成只读
- **澄清向导**：工具调用意图不明时拦截并弹出多步澄清卡片（A/B/C + 自定义），答完注入上下文放行、可跳过；精准调用零打扰
- **授权与审批**：prod 环境仅认预授权 allowlist（禁临时审批），service stop/restart 等破坏性动作自动升级 dangerous 需逐次审批；审批卡自动附中文后果说明（未知命令由 LLM 后台补一句回填）；并发审批逐条内联在执行过程流里，可一次全部允许 / 拒绝
- **目标机环境画像**：run 开始自动探测发行版 / init / 包管理器 / 身份并注入系统提示，避免模型凭印象生成与发行版不匹配的命令
- **运维方法论注入**：意图识别输出 `is_change`（变更 / 只读正交维度），按任务性质二选一注入变更纪律 / 只读取证方法论

### 体验与工程

- **键盘优先**：命令面板（`Ctrl+Shift+P`）、页面直达（`Ctrl+Shift+1..8`）、终端与标签快捷键（关闭 / AI 首页走 Shift 档，避开 shell 控制字符占用）
- **外观体系**：9 档主题（深色 / 浅色 / 跟随系统 / 蓝屏 / 森林 / 薄荷 / 蜜蜂 / 珊瑚 / 手记）× 10 套强调色，渐变模式与方向可配；旧键值经 `asEnum` 白名单闸门回落，不设兼容分支
- **内置运维技能**：`linux-ops-triage` / `nginx-service-diagnose` / `ops-change-safety`
- **CI 门禁**：`npm run ci` = 后端 pytest + 前端 typecheck + vitest + electron-vite 构建 + 网关字节漂移自检；`.github/workflows/ci.yml` 是同口径的 GitHub Actions 配置（仓库托管 Gitee，推到 GitHub 即生效；Windows job 额外做内嵌 Python 准备与 dir 产物 + tar.gz 出件）

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
| 打包 | electron-builder | dir 产物 + tar.gz 直发（Windows）|

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
│  ┌─ Renderer (Vue 3) ─┐    ┌─ Main (Node.js) ─────┐  │
│  │  SSH 终端 + 任务台  │    │  node-pty (本地终端)  │  │
│  │  Monaco Editor      │    │  ssh2 (远程终端+隧道)  │  │
│  │  xterm.js / Pinia   │    │  BackendManager       │  │
│  │                     │    │  (spawn python.exe)   │  │
│  │  ← HTTP / SSE →     │    │  (PTY/SSH 会话池)     │  │
│  │  ← Electron IPC →   │    │        ↓              │  │
│  └─────────────────────┘    └─── localhost:port ───┘  │
└───────────────────────┬───────────────────────────────┘
                        │
        ┌───────────────┴────────────────┐
        │     Python FastAPI Backend     │
        │  • Chat 流式对话 (SSE)          │
        │  • Agent Harness 框架          │
        │    - ReAct 决策循环            │
        │    - Reins 拦截器链            │
        │    - Saddle 工具执行           │
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
- **终端 / SSH / SFTP / 隧道**：Renderer → Electron IPC → node-pty / ssh2（纯 Node.js，不经 Python；隧道独立 client 不复用终端会话）
- **Agent 远程执行**：Python 后端 → WS 反向通道（`/ws/bridge`，主进程出站拨号、长期保持）→ Electron 主进程 dispatch → ssh2。断线 fail-closed，在途请求绝不重发

## 项目结构

```
cKunAgentSSH/
├── electron/                          # Electron 主进程
│   ├── main.ts                        # 入口：窗口（无边框 + 自定义标题栏）、IPC 注册
│   ├── preload.ts                     # contextBridge 暴露受限 API
│   ├── backend-manager.ts             # Python 进程生命周期（spawn / 健康检查 / 重启）
│   ├── bridge-ws-client.ts            # 与后端 /ws/bridge 的长连接客户端
│   ├── electron-bridge.ts             # WS 请求帧的唯一业务实现（token 生成 + dispatch）
│   ├── pty-manager.ts                 # node-pty 终端实例池
│   ├── ssh-manager.ts / ssh-store.ts  # ssh2 连接池 + SFTP；连接配置 safeStorage 加密
│   ├── tunnel-manager.ts / tunnel-store.ts / tunnel-transport.ts   # SSH 隧道
│   ├── gateway-manager.ts / gateway-store.ts                       # TLS 网关接入
│   ├── github-accel.ts / gh-hosts.ts / gh-download.ts              # GitHub 加速（Git·加速子页）
│   ├── git-repos.ts                   # Git 仓库管理（注册表 + 本地 git 执行）
│   └── desktop-prefs.ts shortcut-win.ts / transports/ / utils/     # 偏好持久化、Windows 快捷方式自建、传输抽象、工具
│
├── src/                               # Vue 3 渲染进程
│   ├── App.vue                        # 根组件：顶栏 + 页面切换
│   ├── layouts/TitleBar.vue           # 标题条与全部功能页入口
│   ├── views/ssh/                     # SshView / AuditView / DashboardView / SchedulesView
│   ├── components/                    # common / agent / chat / home / icons / settings / ssh
│   ├── composables/                   # useSshNavigation（页面清单唯一来源）、useGlobalShortcuts 等
│   ├── stores/                        # Pinia 状态管理
│   └── api/ / styles/ / utils/ / types/
│
├── shared/                            # 主进程 / 渲染进程 / 网关共享模块（端口校验、SSH 错误分类、传输错误码、exec 三态判据、桌面偏好契约、网关帧协议、GitHub 加速纯逻辑、Git 仓库管理纯逻辑）
│
├── backend/                           # Python FastAPI 后端
│   ├── main.py config.py database.py llm_adapter.py prompts.py
│   ├── post_processor.py skill_registry.py builtin_skills.py skills_builtin/
│   ├── logconfig.py model_probe.py netconfig.py safe_tokens.py sse_utils.py win_asyncio_teardown.py
│   ├── routes/                        # chat / agent / schedules / audit / settings / skills / terminal / workflow
│   ├── tests/                         # pytest 测试文件；命名规范与旧名映射见 tests/README.md
│   └── agent/                         # Agent 智能体框架
│       ├── harness/                   # Harness 核心
│       ├── memory/                    # 记忆子系统
│       ├── tools/builtin/             # 34 个内置工具
│       └── intention.py host_profile.py bridge_relay.py schedule_engine.py
│
├── docs/superpowers/specs/            # 设计与实施 spec（Git 页、长任务结构化等）
├── gateway/hillgw.mjs                 # 自营 TLS 接入网关（零依赖单文件，独立部署）
├── runtime/                           # 打包用的嵌入式 Python 运行时（本地准备，不入库：.gitignore 整目录忽略）
├── scripts/                           # dev / package / clean / ci-check / deploy（deploy/ = 目标机一键部署脚本）
└── package.json
```

## 核心模块说明

### Electron 主进程

- **main.ts** — 窗口创建（无边框 + 自定义标题栏）、IPC handler 注册
- **preload.ts** — `contextBridge` 暴露受限 API
- **backend-manager.ts** — Python 进程生命周期：spawn、健康检查、自动重启
- **pty-manager.ts** — node-pty 实例池，跨平台 shell 检测
- **ssh-manager.ts / ssh-store.ts** — ssh2 连接池（密码 / 密钥认证、SFTP 断点续传）；连接配置 safeStorage OS 级加密
- **electron-bridge.ts / bridge-ws-client.ts** — 与后端 `/ws/bridge` 的请求通道实现

### 渲染进程

- **App.vue** — 根组件：窗口与顶栏，接收 `TitlebarPage` 并驱动导航高亮
- **TitleBar.vue** — 标题条与全部功能页入口，分组与高亮见「功能地图」
- **SshView.vue** — SSH 主视图的页面外壳：页面清单以 `useSshNavigation.NavPage` 为唯一来源，多页 `v-show` 切换 + 首访惰性挂载；顶栏「AI」与 `Ctrl+Shift+B` 都回到首页任务台
- **components/ssh/** — 终端、SFTP、Docker、隧道、监控、批量下发等页面级组件；`stores/` 承载 Pinia 状态，`docker-core.ts` 等纯核心层无框架依赖

### Python 后端

- **main.py** — FastAPI 入口，CORS 配置，路由注册（含 `/ws/bridge`）
- **database.py** — SQLite 操作层
- **llm_adapter.py** — OpenAI 兼容流式客户端
- **routes/** — API 路由（chat、agent、schedules、audit、settings、skills、terminal、workflow）

### Agent 框架

采用马术装备隐喻设计：

- **Driver** — ReAct 决策循环主驱动
- **Reins** — 16 个拦截器实例（默认装配；关 `enable_dynamic_budget` 则 15，定时任务走的就是这条。清单以 `backend/agent/harness/reins/defaults.py` 的 `create_default_reins` 为准）：技能注入、循环检测、权限审批、文件系统守护、澄清向导、墙钟熔断、成本熔断、事后复盘、计划模式等
- **Bridle** — 上下文组装 + 压缩
- **Saddle** — 工具执行 + 沙箱（34 个内置工具）
- **Stirrups** — LLM 调用 + 重试

## 开发

### 前置条件

- Node.js 20+
- Python 3.11+
- 可访问的 OpenAI 兼容 LLM 服务（vLLM / Ollama / OpenAI）

### 首次启动的模型配置

首次启动**不会预填任何模型**，请在 设置 → 模型 中添加你的 LLM 端点。

开发者 / 演示环境可在 `backend/.env` 中显式开启首启自动探测：

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
npm install                                   # 前端依赖
cd backend && pip install -r requirements.txt && cd ..   # Python 依赖
npm run dev
```

测试依赖另装：`pip install -r backend/requirements-dev.txt`。

### 命令

```bash
npm run dev            # 开发模式（Vite HMR + Electron）
npm run dev:web        # 开发模式 + dev server 监听 0.0.0.0（unix 用 dev:web:unix）
npm run dev:backend    # 单独启动 Python 后端 —— 注意：这条同样绑 0.0.0.0
                       # （与 dev:web 配套，供手机在同一局域网里访问；
                       #  只要本机能用就改成 `npm run dev:backend -- --host 127.0.0.1`，
                       #  main.py 自己的默认值是 127.0.0.1）
npm run build          # 构建生产版本
npm run typecheck      # 前端类型检查
npm test               # 前端单测（vitest）
npm run test:backend   # 后端单测（pytest）
npm run ci             # 本地门禁（五项，与 CI 同口径）
npm run package        # electron-builder 打包
npm run clean          # 清理 electron-builder 产物（dist / dist-*）与中间配置
npm run preview        # 预览构建结果
```

### 打包与分发

- **解释器解析顺序、内嵌 Python 运行时准备**：唯一来源是
  [`scripts/package.js`](scripts/package.js)（构建期探活口径：`CKUN_PYTHON`、
  Windows `runtime/python.exe`、POSIX `runtime/bin/python3`）与
  [`electron/backend-manager.ts`](electron/backend-manager.ts)（运行期解析顺序与打包后的
  `resources/python-runtime/`）。`runtime/` 目录按 `.gitignore` 整体忽略，需本地准备。
- **图标**：`resources/icon.png`
- **代码签名**：未配置证书
- **发布形态（tar-only， 2026-10-23 决策）**：不再出 nsis/portable 安装器；`npm run package` 由 electron-builder 出 `dist/win-unpacked/`，
  再由 `scripts/package.js` 压成 `dist/ckunagentssh-<version>-win32-x64.tar.gz`（解包目录直发）。dir 产物没有安装器职责，
  桌面/开始菜单快捷方式与 AUMID 通知归属由 `electron/shortcut-win.ts` 首启自建。

## License

[MIT](LICENSE)
