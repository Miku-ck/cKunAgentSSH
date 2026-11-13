# cKunAgentSSH 结构重构方案（待功能提交后执行）

> **执行前提**：本文档所有重构动作都涉及大规模文件移动与 import 路径变更，**必须在当前进行中的 experts/plugins 功能提交后执行**。当前工作区有 54 个文件未提交修改（+3183/-501 行）、32 个未跟踪文件，并行开发活跃（最新 mtime 距评估时仅 20 分钟）。
>
> **评估结论**：项目结构整体规范，分层清晰，扩展稳定性良好（4.1/5.0）。本方案针对 3 个 P0/P1 问题，按「零冲突增量」原则设计。

---

## 一、已完成项（2026-11-10）

| 项 | 内容 | 验证 |
|---|---|---|
| 临时文件清理 | `.tmp-check/` 已由并行开发同事清理 | ✅ 目录不存在 |
| 文档归属确认 | `docs/` 是有意保留的空占位（README 第 239 行、DESIGN.md 第 67 行明确说明），保持原位 | ✅ 无引用破坏 |
| `backend/pyproject.toml` | 补包元数据（name/version/requires-python/build-system），**不重复** ruff/pytest 配置（避免优先级冲突） | ✅ tomllib 语法验证通过 |
| 基线验证 | 前端测试 193 文件 / 2332 测试全通过；electron-vite build 成功 | ✅ 基线健康 |
| `backend/db/` 包（2026-11-11） | P0 阶段 1-2：`_conn` / `_util` / `experts` / `plugins` 骨架与试点，函数体原样复制，`database.py` 仍是公共入口 | ✅ 冒烟校验与 database 行为一致；相关 pytest 全过 |
| P0 阶段 3 增量（2026-11-12） | `conversations`（会话/消息/工具配置）与 `skills`（技能/按模式配置）迁入 `db/` 包，`database.py` 仍是公共入口；**顺带修复 `get_messages` 的先在缺陷**——外层 `ORDER BY rowid` 引用派生表不存在的列（SQLite 3.51 实测必错，此前无测试覆盖），两处同步改为内层显式投影 `rowid AS msg_rowid`，新增 `tests/test_conversation_messages_paging.py` 回归 | ✅ 冒烟校验通过；lint 与相关 pytest 全过 |
| P0 阶段 3 增量（2026-11-12） | `schedules`（定时任务 + 执行历史）迁入 `db/schedules.py`；`prune_agent_runs` / `prune_conversations` 等跨域清理维护留待对应域迁移时一并处理 | ✅ 冒烟校验通过（含 time_hour=0 不被 or 吞掉等边界） |
| P0 阶段 3 修复（2026-11-12） | 代码审查后修复：① `database.init_db` 把库路径同步给 `db._conn._db_path`，`db/_conn._get_conn` 加未初始化 fail-fast 守卫（此前 `db.list_schedules()` 直接调用命中 `sqlite3.connect("")` 临时空库，实测 `no such table`）；② `clear_schedule_runs` 进行中闸下沉为 BEGIN IMMEDIATE 原子操作（消除路由层两次 to_thread 之间的 TOCTOU 窗口），闸拒绝返回 `None`、路由按 None 回 409；③ `prune_schedule_runs` 只在终态行里数序号 + `rowid DESC` tie-break；④ `update_schedule_run` 拆为两条单键 UPDATE 并返回受影响行数（替代 `WHERE id = ? OR run_id = ?`）；⑤ `create_schedule` 签名诚实化为 `Optional[dict]`，路由层 None → 404；⑥ `db/schedules.py` 钉 `__all__`；⑦ 新增 `tests/test_db_schedules_parity.py`（16 函数 AST parity + 共享池 CRUD 回归 + 未初始化 fail-fast），迁移期两份实现漂移由 CI 拦截 | ✅ parity + schedules 域 14 个测试文件 132 项全过 |
| `src/stores/sftp.ts` 拆分（2026-11-12） | P1：文件面拆到 `src/composables/useSftpFiles.ts`、传输面拆到 `src/composables/useSftpTransfer.ts`，store 收敛为会话态 + 门面（1952 → 225 行），公开接口不变 | ✅ typecheck 通过；sftp 相关 66 测试全过 |
| `electron/` 重组（2026-11-12） | P1：根级模块按 core / managers / services / stores 归位，`tunnel-transport.ts` 归入 `transports/`、`ssh-error.ts` 归入 `utils/`，测试跟随源码移动；`main.ts` / `preload.ts` / `preload-helpers.ts` 留根（构建入口不动）；测 shared 核心的 4 个测试移入 `shared/` | ✅ typecheck 清零；electron+shared 68 文件 / 678 测试全过；electron-vite build 成功 |

**基线问题处置**（原「已知基线问题」）：
- `exec-target.test.ts:89` 类型错误：已随专家/插件基线提交（5cc531f）修复，typecheck 全绿。
- 系统 `python` 损坏：本机已恢复可用，全量 pytest 4043 项通过（2026-11-11）。

---

## 二、P0：拆分 `backend/database.py`（5442 行，194 个顶层函数）

### 现状分析

- **30 张表**，194 个顶层函数，全部通过 `_get_conn()` 拿连接
- **50 个文件、61 处** `from database import X` 引用
- **跨域依赖极少**：各领域函数基本自包含，仅少量同域内互调（如 `create_expert` → `get_expert`）

### 目标结构

```
backend/db/
├── __init__.py          # 再导出全部公共函数（保持 `from database import X` 兼容）
├── _conn.py             # 连接池、_get_conn、_close_all_connections、init_db、迁移入口
├── _util.py             # _now, _new_id, _assert_sql_ident, _assert_sql_ident_list, _mask_api_key
├── _secrets.py          # _dpapi_transform, protect_secret, unprotect_secret
├── conversations.py     # 会话与消息（create/list/get/update/delete_conversation, add/get_messages 等）
├── skills.py            # 技能与模式配置（list/create/update_skill, upsert_skill_mode_config 等）
├── experts.py           # 专家（list/get/create/update/delete_expert）
├── plugins.py           # 插件（list/get/create/update/consent/delete_plugin 等）
├── models_config.py     # 模型配置（list/get/create/update/delete_model_config, get_default_model 等）
├── bot.py               # Bot 全部（人格/渠道/会话/消息/关系/状态/主动/画廊，约 60 个函数）
├── agent.py             # agent_runs, tool_calls, agent_jobs, agent_strategies
├── memory.py            # memories V2 迁移逻辑（_init_memory_v2, _migrate_memory_v1_to_v2）
├── settings.py          # app_settings, tools_config, 策略读取（get_http_policy, get_memory_settings 等）
├── schedules.py         # schedules, schedule_runs
└── audit.py             # audit_log
```

### 实施步骤（零冲突增量）

**阶段 1：建骨架（不改现有代码）**

1. 建 `backend/db/` 目录，写 `db/__init__.py`：
   ```python
   """数据访问层 — 按领域拆分的 repository 包。
   
   公共入口仍是 backend/database.py（再导出），本包供新代码按需引用。
   """
   from db.conversations import *  # noqa: F401,F403
   from db.skills import *
   # ... 其余领域
   ```

2. 写 `db/_conn.py`：复制 `_PooledConnection`、`_create_connection`、`_get_conn`、`_close_all_connections`、`_schema_is_current`、`_assert_sql_ident`、`_assert_sql_ident_list`

3. 写 `db/_util.py`：复制 `_now`、`_new_id`、`_mask_api_key`

4. 写 `db/_secrets.py`：复制 `_dpapi_transform`、`protect_secret`、`unprotect_secret`

**阶段 2：迁移一个领域试点（建议 experts）**

5. 写 `db/experts.py`：复制 `_validate_expert_slug`、`list_experts`、`get_expert`、`get_expert_by_slug`、`create_expert`、`update_expert`、`set_expert_enabled`、`delete_expert`
   - 顶部 import：`from db._conn import _get_conn, _assert_sql_ident`
   - **不改函数体逻辑**

6. 写 `db/plugins.py`：同法迁移 plugins 相关 12 个函数

7. 在 `db/__init__.py` 加 `from db.experts import *` / `from db.plugins import *`

8. **验证**：`python -c "from db.experts import list_experts; print('OK')"`

**阶段 3：逐步迁移其余领域**

9. 按「Bot → 会话 → 技能 → 模型 → Agent → 设置 → 定时 → 审计 → 记忆」顺序，每次迁移一个领域
10. 每迁移一个领域，跑一次 `npm run test:backend`（需先修复 Python 环境）
11. 迁移完成后，`database.py` 保留为**再导出壳**：
    ```python
    """向后兼容入口 — 实现已迁至 db/ 包。
    
    新代码请用 `from db.X import Y`；本模块仅为既有 50 处引用保留。
    """
    from db import *  # noqa: F401,F403
    ```

### 风险与回滚

| 风险 | 缓解 |
|---|---|
| 迁移中改错函数体 | 只复制不改；每步验证 |
| `_get_conn` 连接池状态丢失 | `_conn.py` 保持单例模式不变 |
| 迁移后 `from database import X` 失效 | `database.py` 保留再导出壳 |
| 迁移顺序错误导致循环 import | 严格按 `_conn` → `_util` → `_secrets` → 领域顺序 |

### 验收标准

- [ ] `python -c "from database import list_experts"` 与 `python -c "from db.experts import list_experts"` 都成功
- [ ] 后端 pytest 全通过（228 文件）
- [ ] `database.py` 行数 < 50 行（只剩再导出）
- [ ] `db/` 各文件行数 < 800 行（bot.py 可能最大）

---

## 三、P1：拆分 `src/stores/sftp.ts`（1952 行，51 处引用）

### 现状分析

- 单 store 承载：连接状态、文件列表、传输队列、排序过滤、冲突处理、chmod/复制
- 51 处引用，改动影响面大
- 与 `composables/` 职责重叠

### 目标结构

```
src/
├── stores/
│   └── sftp.ts                    # 只保留：连接状态、当前路径、传输队列（约 800 行）
├── composables/
│   ├── useSftpFiles.ts            # 文件列表、排序、过滤、选中态（新增，约 400 行）
│   └── useSftpTransfer.ts         # 传输控制、冲突处理、重试（新增，约 500 行）
```

### 实施步骤

1. **先补测试**（本次未提交修改已新增 `sftp.test.ts` 104 行，需扩展到覆盖排序/过滤/传输）
2. 从 `sftp.ts` 提取 `useSftpFiles`：
   - `localEntries`、`remoteEntries`、`localSort`、`remoteSort`、`localFilter`、`remoteFilter`
   - `sortEntries`、`filterEntries`、`setSort`
3. 从 `sftp.ts` 提取 `useSftpTransfer`：
   - `transfers`、`isLive`、`localCopy`、`remoteChmod`
   - 上传/下载/冲突解决
4. `sftp.ts` 改为组合两个 composable：
   ```ts
   export const useSftpStore = defineStore('sftp', () => {
     const files = useSftpFiles()
     const transfer = useSftpTransfer()
     // 连接状态保留在 store
     return { ...files, ...transfer, /* 连接状态 */ }
   })
   ```

### 风险与回滚

| 风险 | 缓解 |
|---|---|
| 51 处引用受影响 | 保持 `useSftpStore` 接口不变（组合式返回） |
| 测试覆盖不足 | 先补测试再拆 |
| composable 间状态共享 | 用 store 作为单一状态源，composable 只读/写 |

### 验收标准

- [ ] 前端 typecheck 全通过（除既有 `exec-target.test.ts:89` 错误）
- [ ] 前端 vitest 全通过（193 文件 / 2332 测试）
- [ ] `sftp.ts` 行数 < 900 行
- [ ] `useSftpFiles` / `useSftpTransfer` 有独立单测

---

## 四、P1：重组 `electron/` 根级模块（27 源码 + 61 测试平铺）

### 现状分析

- `electron/` 根级：27 个源码文件、61 个测试文件
- 测试文件与源码**强绑定**（`ssh-manager.ts` 有 12 个 `ssh-manager-*.test.ts`）
- 测试文件相对 import 43 处，源码文件相对 import 66 处
- `handlers/` 只有 1 个文件却单独建目录

### 目标结构

```
electron/
├── core/               # main.ts, crash-reporter.ts, diagnostics.ts, desktop-prefs.ts, main-logger.ts
├── managers/           # backend-manager.ts, ssh-manager.ts, pty-manager.ts, tunnel-manager.ts, updater.ts
├── services/           # github-accel.ts, gh-download.ts, gh-hosts.ts, git-repos.ts
├── stores/             # ssh-store.ts, tunnel-store.ts, gateway-store.ts, upload-task-store.ts
├── handlers/           # fs-handler.ts（扩为多个 IPC handler）
├── transports/         # 保持（gateway-transport.ts, ssh-transport.ts, tunnel-transport.ts）
├── utils/              # 保持（14 个文件）
└── [测试文件跟随被测源码移动]
```

### 实施步骤

1. **先批量替换 import 路径**（用脚本，一次性改完）：
   ```bash
   # 示例：main.ts 从 electron/core/ 移入后，所有 `./main` 引用要改
   # 建议用 codemod 脚本批量处理，避免手工遗漏
   ```

2. 按「core → managers → services → stores」顺序移动，每次移动后：
   - 更新所有相对 import
   - 跑 `npm run typecheck`
   - 跑受影响的测试文件

3. 测试文件**跟随被测源码**移动（保持同目录）

4. 更新 `tsconfig.node.json` 的 `include`（路径不变，仍 `electron/**/*`）

5. 更新 `electron-builder.yml` 的 `files` 白名单（`main` 入口路径改为 `out/main.js`，不变）

### 风险与回滚

| 风险 | 缓解 |
|---|---|
| import 路径遗漏 | 用脚本批量替换 + typecheck 兜底 |
| 测试文件找不到被测模块 | 测试跟随源码移动 |
| 打包入口变化 | `electron.vite.config.ts` 的 `main` 入口指向 `out/main.js`，不受目录结构影响 |
| 与并行开发冲突 | **必须在功能提交后执行** |

### 验收标准

- [ ] 前端 typecheck 全通过
- [ ] 前端 vitest 全通过
- [ ] electron-vite build 成功
- [ ] `electron/` 根级源码文件 < 5 个（只剩 `preload.ts` 等必要入口）

---

## 五、执行顺序与时间估算

| 阶段 | 内容 | 预估工时 | 前置条件 |
|---|---|---|---|
| 0 | 当前功能提交 | — | 并行开发完成 |
| 1 | `database.py` 阶段 1（骨架） | 1h | 阶段 0 |
| 2 | `database.py` 阶段 2（experts 试点） | 1h | 阶段 1 |
| 3 | `database.py` 阶段 3（其余领域） | 4h | 阶段 2 + Python 环境修复 |
| 4 | `sftp.ts` 拆分 | 3h | 阶段 0 + 测试补充 |
| 5 | `electron/` 重组 | 4h | 阶段 0 |
| 6 | 全量验证（CI 五项） | 1h | 阶段 3-5 |

**总计约 14 小时**，建议分 3-4 次会话执行，每次一个阶段。

---

## 六、不纳入本次重构的项

| 项 | 原因 |
|---|---|
| `docs/` 迁移 | 有意保留的空占位，README 有说明 |
| 根目录工具态目录（`.mimosa/` 等） | 已 gitignore，不影响仓库 |
| `views/` vs `components/` 职责 | 需在 DESIGN.md 补充说明，非结构问题 |
| `shared/` 层 | 职责清晰，无需调整 |
| 系统 Python 环境修复 | 环境问题，独立于结构重构 |

---

## 七、验证清单（每阶段执行后）

```bash
# 1. 前端类型检查（已知 exec-target.test.ts:89 错误可忽略）
npm run typecheck

# 2. 前端测试
npm run test

# 3. 后端测试（需先修复 Python 环境）
npm run test:backend

# 4. 后端 lint
npm run lint:backend

# 5. 构建
npm run build

# 6. 网关漂移自检
node gateway/hillgw.mjs --selftest

# 7. 清理构建产物
rm -rf out
```
