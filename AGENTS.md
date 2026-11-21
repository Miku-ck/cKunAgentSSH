# AGENTS.md — AI 协作入口

本文件是 AI 编码工具在本仓库的唯一入口。**动手前必读**：

- [CONTEXT.md](CONTEXT.md) — 术语口径（"这个词在这里到底指什么"）
- [DESIGN.md](DESIGN.md) — 界面硬规则（颜色 / 间距 / 布局 / 组件）
- [README.md](README.md) §常用命令 — 命令清单唯一事实源（本文件不复制，避免两边漂移）

## 改动原则

- **先读后改**：改文件前先读相关段落。注释里"曾经…已删"是踩坑记录，别让它复活。
- **最小外科手术**：只做被要求的事，不整文件重写，不顺手重构。
- **口径单一**：同一概念只有一个词、一个入口、一份事实源（见 CONTEXT.md）。
- **单文件单域**：一次改动只碰一个域；拆模块先补测试再拆。

## 硬性禁止

- 不新增 `any`（`as any` / `: any`）。用具体类型；异构数据定义 `interface` / `type` 契约；无法收敛用 `unknown` + 类型守卫。
- 不新增 `@ts-ignore` / `@ts-expect-error`。
- 不写裸空 `catch {}`：清理型忽略（如断开连接）可空，但**必须注释理由**。
- 禁止 `except Exception: pass` / `return 默认值` 且无日志无注释。降级要么 `logger.debug/warning`，要么注释说明为何可忽略。
- 不硬编码密钥 / 令牌 / 内网地址 / 端口：公开面一律占位符（`your-llm-host`、`example.com`），真实值只进 `backend/.env`。
- 不新增 AI 工具配置目录（`.mimosa` / `.superpowers` / `.workbuddy` 等），本文件是唯一入口。
- 不动跨端契约：`backend/tests/test_ai_loop_core_regression.py` 等契约测试钉住的字段与结论取值。
- 不把真实内网信息写进 README / 注释 / UI 占位符 / 示例配置。

## 收尾

- 改完至少跑 `npm run typecheck` + `npm test`；涉及后端加 `npm run test:backend`；提交前跑 `bash scripts/ci-check.sh`。
- 提交前缀 `feat` / `fix` / `refactor` / `chore`，中文描述；不与其他功能混提。
