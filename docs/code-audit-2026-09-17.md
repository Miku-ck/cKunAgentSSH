# 代码审计报告 — cKunAgentSSH（2026-09-17）

审计目标：解释代码量构成、识别冗余/死代码/不相干文件、审计依赖正确性与冗余、评估功能完善度。
方法：全量行数统计（git 跟踪 vs 本地残留）、四维交叉引用死代码扫描（Vue 组件 / stores·composables / electron 模块 / backend 模块）、npm 与 Python 依赖 import 核对、TODO/桩代码扫描、API 端点与工具注册核查。

## 1. 代码量构成 —— 真实规模，无注水

**git 跟踪的真实源码 = 63,451 行；纯生产代码 ≈ 4.8 万行。** "四万多行"由此而来，是三端架构 + 完整 Agent 框架的真实功能量。

| 层 | 生产代码 | 测试代码 |
|---|---|---|
| 前端 `src/`（Vue 3） | 7,644 (.ts) + 15,411 (.vue) = 23,055 | 0（仅 1 个未跟踪新测试） |
| 后端 `backend/`（Python） | 19,975 | 4,439（27 个测试文件） |
| 主进程 `electron/` | 5,574 | — |
| 其他 | 7,255 (package-lock.json) + 1,673 (md) + 324 (sh) + 104 (yml) | — |
| **git 跟踪总计** | **63,451 行** | |

**本地残留（不入 git，gitignore 覆盖完整）**：

- `out/renderer/assets/`：92 个 js chunk、约 70 万行，其中 85 个是 monaco-editor 内置语言定义，另有 28 万行主 bundle。是安装包体积大头，不影响仓库规模。
- `backend/__pycache__/`、`backend/tests/_tmp/`（7 个测试 db）、`.pytest_cache/`、`.benchmarks/`、`node_modules/`：0 个被跟踪。

## 2. 冗余 / 死代码 / 不相干文件 —— 零

| 扫描维度 | 方法 | 结果 |
|---|---|---|
| 41 个 Vue 组件 | import/标签/路径引用交叉扫描 | 全部有引用 |
| stores / composables / utils | `from` 引用扫描 | 全部有引用 |
| electron 模块（8 个非入口） | import 扫描 | 全部有引用 |
| backend 模块（routes/agent 入口外） | import/路由挂载扫描 | 全部有引用 |
| `.workbuddy/` `.trae/` `.benchmarks/` `_bin/` `tests/_tmp/` | git ls-files 核对 | 全部 gitignored，0 个被跟踪 |
| TODO / FIXME / HACK / 501 / NotImplementedError | 全库文本扫描 | 0 处 |

git 历史印证：近期多轮系统性清理（"后端深扫——死函数/重复实现清理"、"全库评审清理"、"补删历史测试残留"）。**无可删死代码或不相干文件。**

## 3. 依赖审计

### npm（12 deps + 12 devDeps）

- 12 个 dependencies 全部在用：xterm 系列（3/2/4 文件）、dompurify、highlight.js、js-yaml、jszip、marked、monaco-editor、node-pty、pinia（11 文件）、ssh2、vue（57 文件）。
- 新增 `vitest` + `jsdom`（未提交）合理，配套 `src/utils/think-scrubber.test.ts`。
- ⚠️ `@types/dompurify` 冗余：dompurify 3.4.11 自带类型（`dist/purify.cjs.d.ts`），可删。
- `@types/js-yaml` 需保留（js-yaml 无自带类型）。

### Python（7 个依赖）

- fastapi（12 文件）、uvicorn、pydantic（6）、openai、httpx（3）全部在用；`python-multipart` 隐式在用（chat.py Form/UploadFile）。
- ⚠️ `trafilatura` 应降为可选依赖：`web_tools.py` 本就是可选导入（ImportError 降级到内置 `_strip_html`），且它拖 lxml 等重依赖。

## 4. 功能完善度 —— 完整，无桩

- 49 个 API 端点全部实现，无 501/NotImplemented 桩。
- 22 个 Agent 工具全部注册（web / ops_file / ops_image / ops_job / ops_obs / ops_schedule / ops_service / shell_exec / spawn_subagent / todo_write / load_skill 等）。
- 拦截器链完整：`reins.py` 含 10 个守卫组件（FilesystemGuard、LoopDetector、ConfidenceMonitor、PermissionGate、AuditLogger、ReflectionRein、DynamicBudgetRein、CostBudgetRein、SkillRein 等），默认链装配 6 个 + 技能注入。
- 核心功能齐全：SSH/SFTP 断点续传、本地+远程终端、Monaco 远程编辑、命令补全、AI Agent（ReAct + 动态预算 + 成本熔断）、审计轮转、调度、技能系统。
- ⚠️ 小瑕疵：README 写"9 拦截器"，实际 10 个守卫类（默认链 6 个），数字需同步。
- ⚠️ 前端测试薄弱：2.3 万行生产代码仅 1 个 vitest 文件，靠 vue-tsc 兜底；后端测试健康（27 文件）。建议给 `stores/sftp.ts`(1140 行)、`electron/ssh-manager.ts`(1922 行) 补单测。

## 5. 精简建议（按收益排序）

| # | 建议 | 收益 | 成本 |
|---|---|---|---|
| 1 | Monaco 语言裁剪：`src/monaco-setup.ts` 全量引入 85 个语言 + 5 个 worker，只留运维常用语言（json/yaml/bash/python/shell）或降纯文本 | out/ 70 万行 → 10 万行内，安装包显著变小 | 低 |
| 2 | 删 `@types/dompurify` | 去冗余依赖 | 一行 |
| 3 | `trafilatura` 移到 `requirements-optional.txt` | 后端安装更轻，降级逻辑已就绪 | 低 |
| 4 | 定期清本地残留（tests/_tmp 7 个 db、__pycache__、.pytest_cache、.benchmarks） | 磁盘整洁 | 低 |
| 5 | README 拦截器数字 9→10 同步 | 文档准确 | 一行 |
| 6 | （可选）前端核心模块补单测 | 质量保障 | 中 |

## 总体评价

无注水、零死代码、依赖近乎零冗余的成熟项目。四万多行是三端 + 完整 Agent 框架的真实功能量，后端测试到位。唯一值得动手的实质瘦身是 monaco 全量语言打包（建议 #1）。
