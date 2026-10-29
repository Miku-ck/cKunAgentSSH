# 后端启动性能优化与 lifespan 重构 — 设计

- 日期：2026-10-29
- 范围：`backend/main.py`、`backend/netconfig.py`、`backend/builtin_skills.py`、
  `backend/database.py::seed_models_async`、新增 `backend/tests/test_startup_timing.py`
- 不在范围：`routes/*` 与 `bot/*` 的模块级导入瘦身（方案 C，另行立项）

## 1. 背景与实测基线

`main.py` 已有 `_STARTUP_T0` + `_perf_mark` 打点体系，但坐标系起点是「依赖 import
完成」，覆盖不到 import 段与 uvicorn 引导段，导致真实启动耗时无法自证。本轮先建仪表
再动刀。

本机实测（Python 3.11.15 / Windows，warm cache，热库与冷库各一次）：

| 阶段 | 实测 | 主要构成 |
|---|---|---|
| `import main` | 448–532ms | `fastapi.openapi.models` 60ms、`routes.bot` 52ms、`main.py` 自身 40ms（应用装配）、`routes.agent` 27ms |
| 模块末尾 → lifespan 进入 | 62–102ms | uvicorn `serve()` → `config.load()`（28ms，导入 loop/protocol/lifespan 实现）+ 事件循环创建 |
| lifespan 内部 | 55ms | `init_db` 17–20ms、batch1 6–9ms、batch2 23–27ms、ready ~2ms |
| └ batch2 大头 | 21.8ms | `agent.harness.reins.gate` 首次导入（发生在 `to_thread` 包着的 `_load_permission_policy` 内） |
| 应用装配 | ~31ms | 12 次 `include_router`（`routes.agent` 10.2ms、`routes.bot` 5.8ms 为大头） |

独立进程对照（含 ~70ms 解释器基线）：`import fastapi` 267ms、`import uvicorn` 128ms、
`import click` 24ms、`uvicorn config.load()` 28ms。**框架导入是本进程启动的绝对大头，
本轮不动。**

由此得到三个必须解决的问题：

1. `logger.info("[cKunAgentic] 后端就绪，import 后初始化耗时 %.0fms", …)` **永远不会落地**：
   它跑在 `logconfig.load_from_db()` 把 root 级别回填成 warning 之后。默认配置下后端对
   自己的就绪耗时零输出，Electron 侧 `backend: health ready` 无从对表。
2. batch2 中除 `seed_models_async` 外，其余三项**只依赖 `init_db`**，却被硬编码排在
   batch1 之后白等一轮；而 batch2 里那 21.8ms 的 `gate` 导入**连 DB 都不依赖**，却被
   压在最后一轮里串行付出。
3. 首启（`models_config` 空 + 配了 `VLLM_BASE_URL`）时两条模型探测**串行**执行，
   最坏 3s×2=6s 全部压在就绪路径上。

## 2. 观测体系：`_MODULE_T0` + 汇总行常驻

### 2.1 双基准

| 常量 | 位置 | 语义 |
|---|---|---|
| `_MODULE_T0` | main.py 第一条可执行语句（所有 import 之前） | 解释器 bootstrap 完成、本模块开始执行。与 Electron `backend: spawned pid=` 同一量级 |
| `_STARTUP_T0` | 保持现状（依赖 import 完成之后） | 现有 4 条明细打点的坐标系，语义不变 |

汇总行同时给出两个相对值：`imports=`（`_STARTUP_T0 - _MODULE_T0`）与
`total=`（now - `_MODULE_T0`），后者可与 Electron 的 `backend: health ready` 直接对表。

### 2.2 节点与输出形态

节点（按发生顺序）：`imports done` → `app assembled` → `serve start` →
`lifespan entered` → `init_db` → `round1` → `background started` → `ready`。

- 明细行：`[perf] <label> @<N>ms`（相对 `_MODULE_T0`），受 `CKUN_PERF=1` 控制，
  级别 **WARNING**。理由沿用现有注释：`logconfig.load_from_db` 会把 root 回填成用户
  配置（默认 warning），INFO 明细会被自己吃掉。
- 汇总行：**常驻，1 行 WARNING**，就绪时输出，形如

  ```
  [perf] startup imports=449ms assemble=31ms boot=29ms lifespan=26ms total=535ms | init_db=18 round1=22 seed=1 bg=1
  ```

  形制与 Electron `electron/utils/perf-mark.ts` 的 `[perf] <name> @<N>ms` 同构，
  且修掉 §1 问题 1 的死日志。

### 2.3 实现约束

`_perf_mark()` 除打印外，还要把 `(label, ms)` 追加进模块级 `_marks` 列表，供汇总行
一次性渲染。汇总行必须在 `yield` 之前输出（health 可服务时刻）。

## 3. lifespan 依赖图：两批串行 → 预导入 + 单轮分派

### 3.1 依赖真相

| 任务 | 依赖 |
|---|---|
| `init_db` | —（必须先完成） |
| `logconfig.load_from_db` / `netconfig.load_policy_from_db` | `init_db` |
| `builtin_skills.sync_packaged_skills` | `init_db` |
| `_load_post_config`（post_processor） | `init_db` |
| `_apply_permission_policy`（模块导入 + DB 回填） | `init_db`（DB 读）+ 三个 reins 模块导入（**无 DB 依赖**） |
| `_reset_stale` | `init_db` |
| `seed_models_async` | `init_db` + netconfig（仅 HTTP 探测段） |

### 3.2 目标结构

```
lifespan:
  _perf_mark("lifespan entered")
  pre_import = create_task(to_thread(_import_permission_modules))   # 纯 CPU 22ms
  init_db(settings.db_path)                                        # 18ms sqlite I/O，与上面重叠
  _perf_mark("init_db done")
  await asyncio.gather(
      to_thread(logconfig.load_from_db),
      to_thread(netconfig.load_policy_from_db),
      to_thread(_sync_skills),             # import 在任务内
      to_thread(_load_post_config),        # import 在任务内
      _apply_permission_policy(),          # async：await pre_import 后再 to_thread 读 DB
      to_thread(_reset_stale),
      _seed_models(),                      # await _net_policy_ready 后调用 seed_models_async
      return_exceptions=True)
  _perf_mark("round1 done")
  _start_background_tasks()
  _perf_mark("background started")
  _perf_mark("ready") + 汇总行
  yield
  ...
```

要点：

- **预导入**：`_import_permission_modules()` 在线程里 `import` 三个 reins 模块并返回
  三个设置函数（`set_allowlist_ttl_hours` / `set_db_enabled` /
  `set_approval_fallback_seconds`），把 21.8ms 纯 CPU 藏进 `init_db` 的 sqlite I/O 窗口。
  该导入在 lifespan 内触发，不进入 `import main` 期，因此不触犯
  `test_startup_critical_path.py::test_import_main_keeps_execution_stack_off_critical_path`
  （它禁的是 `harness.harness/driver/reflection/evolution/tools/httpx`，
  `reins.gate` 不在禁列，测试本身还显式导入过它）。
- **模块导入一律下沉到各自任务内**：`builtin_skills`、`post_processor`、`netconfig` 不再
  在 lifespan 开头集中 import —— 让它们各自在 worker 线程里与 `init_db` 及其他任务重叠。
- **细粒度事件**：模块级 `asyncio.Event`，由 netconfig 任务在 `finally` 中 set；`_seed_models`
  先 `await` 它。这样 seed 的等待值从「整轮 22ms」降到「netconfig 完成 ~3ms」，且
  netconfig 失败也能放行（`load_policy_from_db` 内部已吞 DB 异常）。
- **失败口径不变**：`return_exceptions=True` + 逐条 warning，任何一步失败都不阻断就绪。
- **顺序保证不变**：所有任务在 `yield` 前完成，审批策略依旧先于任何请求处理与
  `PermissionGate` 单例创建写入模块值。
- 为可测性，各步骤抽成模块级函数（`_sync_skills` / `_load_post_config` /
  `_apply_permission_policy` / `_reset_stale` / `_seed_models` /
  `_import_permission_modules`），lifespan 通过模块全局名调用，测试可
  `monkeypatch.setattr(main, …)`。

## 4. netconfig 竞态修复（并发化前置条件）

`netconfig.load_policy_from_db()` 当前第一行就 `_loaded = True`，函数末尾才写
`_policy`。并发化后，探测线程里的 `get_policy()` 可能读到 `_loaded=True` + 默认
`_policy`（`proxy_url=""`）—— 首启探测就此绕过代理。

修复：把 `_loaded = True` 移到 `_policy` 赋值之后。届时 `get_policy()` 只会看到两种
状态：未加载（自己加载，重复一次 DB 读但结果正确）或已完整加载。

现有测试（`test_http_env_precedence.py` / `test_http_log_diagnostics.py` /
`test_proxy_clear_persistence.py`）只用 `_loaded` 做保存/恢复，不受影响。

## 5. 技能同步幂等短路

`sync_packaged_skills()` 每次启动对每个内置技能 `upsert_builtin_skill` 一次，并在
`upserted > 0` 时 `skill_registry.reload(force=True)`。稳态下这是纯冗余写。

规则（三条件同时满足才跳过写库与 reload）：

1. 扫描结果摘要 `digest` 与状态文件记录一致。`digest` = 对「按 slug 升序逐行拼接的
   `slug|name|description|category|trigger_words|system_prompt`」取 sha256 前 16 位；
2. 状态文件记录的 slug 集合全部仍存在于 `db.list_builtin_skill_slugs()` 结果中
   —— 防「DB 被删/重建但状态文件还在」导致内置技能永不再写入；
3. 本次扫描非空（沿用现有「空扫描 ≠ 技能被删光」的保护）。

无论是否跳过，`_managed_slugs` 都必须照常填充（孤儿清理豁免名单
`get_managed_slugs()` 与 `previous - managed` 判据依赖它）。

状态文件结构升级：`{"version": 2, "slugs": [...], "digest": "..."}`。
v1 文件无 `digest` → 自然走一次全量同步并升版，向后兼容，无需迁移代码。

跳过时打一条 debug/明细打点 `skills=skipped`，让「为什么没写库」可查。

## 6. 模型探测：并发两条

`database.seed_models_async`：

- 稳态（`models_config` 非空）：立即返回，实测 0.8ms，不动。
- 首启：`_probe_first_model(base_url)` 与 `_probe_first_model(thinking_url)` 当前串行
  `await asyncio.to_thread`，最坏 3s×2=6s。改为两条探测 `asyncio.gather` 并发，
  写入仍逐条独立 `try` + 即时 `commit`（保留 R2 修复：单条 `IntegrityError` 不连坐另一条）。
- 仍留在就绪路径上：首启 UI 立刻要读 `models_config`，挪到后台会造成「首次进入设置页
  模型列表为空、刷新后才出现」的回归。
- 探测失败/超时语义不变（`model_probe._probe_first_model` 内部已全吞异常）。
- 两条 URL 相同（含 `rstrip('/')` 后相等）时仍只探测一次。

## 7. 冗余清理

| 位置 | 处理 |
|---|---|
| `__main__` 内 `import asyncio` | 删（模块顶层已有） |
| lifespan 内 `import logconfig` | 删（模块顶层已导入） |
| lifespan 内 `import database as _db` | 统一为模块顶层 `import database as db`，全文件用 `db.` |
| `agent.bridge_relay` 两处 import | 合并到模块顶层一次 |
| `post_processor` / `schedule_engine` 各自 import 两次 | 各收敛为一处 |
| CORS 段「历史补偿中间件已移除」整段注释 | 删（4 行在解释一段不存在的代码） |
| `api_token_auth` 里 `/api/health` 那段自相矛盾插话 | 压成一行事实：`/api/health` 是唯一免鉴权端点（且它是唯一被注册的健康端点） |
| `win_asyncio_teardown.install()` | 时机不变（`uvicorn.Config()` 之前、任何 transport 创建之前），补一行「若新增其他入口需同步调用」 |
| 关停序列 | `tick.stop` / `schedule_engine.stop` / `proactive.stop` 三个独立 stop 并发 `gather`；顺序保证不变：先取消 `restore_task` 并等待、渠道 `stop_all` 仍在最前 |

不动的部分：CORS 白名单与其安全论证、`hmac.compare_digest` 的 bytes 比较、
`AuditWriteError → 503` 异常处理器 —— 它们有明确的历史事故背书，属有效注释与有效防御。

## 8. 测试

新增 `backend/tests/test_startup_timing.py`：

1. `test_db_read_steps_run_in_one_round`：把 6 个步骤 monkeypatch 成
   `time.sleep(0.04)`，跑一次 lifespan，断言总耗时 < 0.15s（串行会 ≥ 0.24s）。
   证明「单轮分派」不退化。
2. `test_ready_summary_is_always_logged`：`caplog` at WARNING 断言汇总行存在，且包含
   `imports=`、`init_db=`、`round1=`、`total=`。
3. `test_perf_detail_lines_require_env`：默认无 `[perf] … @Nms` 明细行；
   `monkeypatch.setattr(main, "_PERF_VERBOSE", True)` 后出现。
4. `test_seed_only_waits_for_net_policy`：netconfig 步骤 40ms、skills 步骤 400ms，
   断言 `seed_models_async` 被调用的时刻早于 skills 完成时刻（细粒度事件生效）。
5. `test_lifespan_teardown_runs_on_body_exception`：应用体抛异常时 `_stop_background_tasks`
   仍被调用（裸 `yield` 后的收尾代码在 throw 时会被跳过，后台循环与渠道会成孤儿）。

lifespan 测试需 patch 掉外部副作用：`init_db`、渠道 `get_channel_runtime`、
`get_global_tick`、`get_schedule_engine`、`bot.proactive.get_scheduler`、
`set_bridge_relay`、`post_processor.get_post_process_manager`。

回归必跑：
`pytest tests/test_startup_critical_path.py tests/test_cors_preflight.py
tests/test_bot_routes.py tests/test_http_env_precedence.py
tests/test_http_log_diagnostics.py tests/test_proxy_clear_persistence.py`
以及 `python -X importtime -c "import main"` 人工复核导入图未回退。

## 9. 实测收益与残留

实施后同机实测（`python main.py --port 7791` 真启动 3 次 + 冷进程 `import main` 3 次）：

| 项 | 前 | 后（实测） |
|---|---|---|
| lifespan 内部（entered → ready） | 55ms | 32 / 38 / 34ms |
| 其中 init_db | 17–20ms | 2–6ms（同一库，二次启动） |
| 其中回填轮次 | batch1 6–9ms + batch2 23–27ms（串行） | round1 28–31ms（单轮并发） |
| 后台组件启动 | 计入 batch2 | 1ms |
| 技能同步稳态 | 3 次 upsert + reload | 短路：不写库、不 reload 注册表 |
| 首启模型探测 | 最坏 6s | 最坏 3s（两条探测并发） |
| 后端就绪耗时可见性 | 0（死日志） | 1 行常驻汇总 + `CKUN_PERF=1` 明细 |
| `import main`（冷进程） | 448–532ms | 427–437ms |

实测汇总行（第 2 次）：

```
[perf] startup imports=393ms assemble=38ms boot=19ms lifespan=32ms total=484ms
       | init_db=2ms round1=28ms background=1ms
```

读法：`imports` 从 `main.py` 第一行起算；`total≈imports` 说明就绪时刻几乎完全由导入
决定。同一进程墙钟到 `/api/health` 为 1.0–1.05s，差额 ~540ms 是解释器 bootstrap +
venv site 扫描，早于 `_MODULE_T0`，不在本轮范围（方案 B：lifespan + 装配/引导）。

残留（本轮不动，已可视化）：`import main` 中 `fastapi` 267ms / `uvicorn` 128ms 为
框架固定成本，我方模块仅 `routes.bot` 52ms、`routes.agent` 27ms 可谈、`config.load()`
28ms、`include_router` 合计 31ms。

期望管理：Electron 健康轮询粒度为 150–500ms，后端省下的二十几毫秒在用户感知上大概率
被轮询粒度吞掉；本轮的真实价值是「后端自报耗时第一次可与 Electron 观测对表」，以及
对「步骤串行叠加」这类会随任务增加而复发的结构性问题的消除。
