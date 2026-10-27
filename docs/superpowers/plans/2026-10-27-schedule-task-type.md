# 定时任务双类型（运维 / 日常）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给定时任务增加「运维任务 / 日常任务」双分类，默认运维，差异仅体现在模板集合、列表分区与卡片色条。

**Architecture:** 数据层新增 `task_type` 列（'ops'|'daily'，DEFAULT 'ops'，存量自动归运维）；REST 与 AI 工具透传该字段但保持类型不可变；调度引擎、审批、gate 零改动；前端弹窗标题区放类型分段开关、列表按类型分区、卡片用主题 token 色条区分。

**Tech Stack:** Python / FastAPI / SQLite / Vue 3 (Composition API, `<script setup lang=ts>`) / Vitest / pytest。

**Spec:** `docs/superpowers/specs/2026-10-27-schedule-task-type-design.md`

## Global Constraints

- 类型取值仅 `'ops'` | `'daily'`，缺省与存量一律 `'ops'`；**编辑已存任务不允许改类型**。
- 调度引擎（`backend/agent/schedule_engine.py`）、审批 gate、环境解析链路**一律不改**。
- 前端颜色**禁止硬编码**，运维沿用 `--accent-primary`、日常沿用 `--accent-green`（均为主题 CSS 变量）。
- 入库上限沿用现值：`_NAME_MAX_LEN = 100`、`_TASK_PROMPT_MAX_LEN = 50_000`，前后端同口径（S7-11）。
- 共享工作树：工作区含大量他人 WIP，**只精准提交本计划触及的 5 个文件 + 新增测试文件**，不碰其余。
- 命令：后端 `npm run test:backend`（pytest）；前端 `npm run typecheck`、`npm test`（vitest）。

---

### Task 1: 数据层新增 task_type 列与读写透传

**Files:**
- Modify: `backend/database.py:383-402`（CREATE TABLE schedules）
- Modify: `backend/database.py:473-480`（_migrate_db 补列分支）
- Modify: `backend/database.py:2632-2636`（_SCHEDULE_COLS）
- Modify: `backend/database.py:2673-2706`（create_schedule INSERT）
- Test: `backend/tests/test_schedule_task_type.py`（新建）

**Interfaces:**
- Produces: `schedules.task_type TEXT NOT NULL DEFAULT 'ops'`；`db.create_schedule(data)` 读 `data.get("task_type")`；`db.list_schedules()/get_schedule()` 返回含 `task_type` 键。`update_schedule` 的 `allowed` 集合**不含** task_type（类型不可变，即便 patch 带也被 db 层丢弃）。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_schedule_task_type.py`：

```python
# spec 2026-10-27-schedule-task-type
import database as db


def _make(conn_env, **over):
    data = {
        "name": "t", "schedule_type": "interval", "interval_minutes": 60,
        "time_hour": 9, "time_minute": 0, "weekday": 0, "day_of_month": 1,
        "target_host": "", "task_prompt": "p", "enabled": 1,
        "next_run_at": "2099-01-01 00:00:00",
    }
    data.update(over)
    return db.create_schedule(data)


def test_default_task_type_is_ops(base_db):
    row = _make(base_db)
    assert row["task_type"] == "ops"


def test_create_daily_task_type(base_db):
    row = _make(base_db, task_type="daily")
    assert row["task_type"] == "daily"


def test_update_cannot_change_task_type(base_db):
    row = _make(base_db, task_type="ops")
    updated = db.update_schedule(row["id"], {"task_type": "daily"})
    assert updated["task_type"] == "ops"
```

> `temp_db` fixture：沿用 `backend/tests/` 现有建库 fixture（参考同目录 `test_schedule_entry_bounds.py` 的库初始化方式）。若无现成 fixture，用 `db.init_db(tmp_path/…)` 建临时库。

> **真实 fixture（直接复制到新测试文件头部）**：`backend/tests/` 无全局 db fixture，每个测试文件自带。参考 `test_schedule_routes.py:49-70`：
>
> ```python
> import os, pytest, database as db
>
> @pytest.fixture
> def base_db(tmp_workspace):          # tmp_workspace 来自 conftest.py
>     p = os.path.join(tmp_workspace, "schedules.db")
>     db._close_all_connections(); db.init_db(p)
>     yield p
>     db._close_all_connections()
> ```
>
> 下文所有 `temp_db` 均换名为 `base_db`。REST 测试的 `client` fixture 同样参考 `test_schedule_routes.py:58-70`（含 monkeypatch 掉 `compute_next_run`/`get_schedule_engine`）。

- [ ] **Step 2: 运行确认失败**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: FAIL —KeyError 'task_type' 或列不存在

- [ ] **Step 3: CREATE TABLE 加列**

`backend/database.py:394` 的 `enabled INTEGER DEFAULT 1,` 之前插入一行：

```sql
                task_type       TEXT NOT NULL DEFAULT 'ops',
```

- [ ] **Step 4: _migrate_db 补列**

在 `backend/database.py` `_migrate_db` 内、`last_abnormal_at` 补列分支旁（约 :478-480）追加同模式：

```python
    if "task_type" not in schedule_cols:
        conn.execute(
            "ALTER TABLE schedules ADD COLUMN task_type TEXT NOT NULL DEFAULT 'ops'"
        )
```

> `schedule_cols` 已在 :478 由 PRAGMA table_info 取得，直接复用。

- [ ] **Step 5: _SCHEDULE_COLS 加字段**

`backend/database.py:2633` 首段列串改为含 task_type：

```python
_SCHEDULE_COLS = (
    "id, name, task_type, schedule_type, interval_minutes, time_hour, time_minute, "
    ...
)
```

- [ ] **Step 6: create_schedule INSERT 落 task_type**

`backend/database.py:2679-2701`：列名清单在 `name,` 后加 `task_type,`，占位符加一个 `?`，值元组在 name 值后加：

```python
                (data.get("task_type") or "ops"),
```

- [ ] **Step 7: 运行确认通过**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add backend/database.py backend/tests/test_schedule_task_type.py
git commit -m "feat(schedule): schedules 表新增 task_type 列并读写透传"
```

---

### Task 2: REST 层校验类型、拒绝改类型

**Files:**
- Modify: `backend/routes/schedules.py:30-44`（ScheduleIn）
- Modify: `backend/routes/schedules.py:47-62`（SchedulePatch）
- Modify: `backend/routes/schedules.py:114-139`（update_schedule 守卫）
- Test: `backend/tests/test_schedule_task_type.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `db.create_schedule` 读 task_type、`update_schedule` 丢弃 task_type。
- Produces: `POST /schedules` 接受 `task_type`（Literal ops|daily，默认 ops，非法 422）；`PATCH /schedules/{id}` 对已存行传入不同 task_type → 422。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_schedule_task_type.py` 追加（沿用同文件已有的 TestClient/httpx fixture；参考 `backend/tests/` 中现成的 REST 测试客户端用法）：

```python
# spec 2026-10-27-schedule-task-type
def test_post_invalid_task_type_422(client):
    r = client.post("/api/schedules", json={
        "name": "x", "task_type": "weekly"})
    assert r.status_code == 422


def test_post_daily_roundtrip(client):
    r = client.post("/api/schedules", json={"name": "x", "task_type": "daily"})
    assert r.status_code == 200 and r.json()["task_type"] == "daily"


def test_patch_change_type_rejected(client):
    sid = client.post("/api/schedules", json={"name": "x"}).json()["id"]
    r = client.patch(f"/api/schedules/{sid}", json={"task_type": "daily"})
    assert r.status_code == 422
```

> `client` fixture 已含 `base_db` 依赖与懒导入补丁（见 Task 1 注），本文件内定义一份即可复用。

- [ ] **Step 2: 运行确认失败**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: FAIL（422 断言不成立 / task_type 被忽略）

- [ ] **Step 3: ScheduleIn 加字段**

`backend/routes/schedules.py` 顶部 import 补 `from typing import Literal, Optional`（已有 Optional，追加 Literal）。`ScheduleIn`（:33 后）加：

```python
    task_type: Literal["ops", "daily"] = "ops"
```

- [ ] **Step 4: SchedulePatch 加字段**

`SchedulePatch`（:52 后）加：

```python
    task_type: Optional[Literal["ops", "daily"]] = None
```

- [ ] **Step 5: PATCH 拒绝改类型**

`update_schedule`（:119 `_drop_nulls` 之后、:120 merged 之前）插入：

```python
    patch = _drop_nulls(req.model_dump(exclude_unset=True))
    # spec 2026-10-27-schedule-task-type：类型创建后不可变，传入不同值直接 422
    req_type = req.model_dump(exclude_unset=True).get("task_type")
    if req_type is not None and req_type != existing.get("task_type"):
        raise HTTPException(status_code=422, detail="任务类型创建后不可修改")
    patch.pop("task_type", None)  # 相同值也丢弃，避免进入下游 merged 校验
    merged = {**existing, **patch}
```

> 注意：Task 1 已让 `db.update_schedule` 丢弃 task_type，这里 pop 是双保险并保证 `merged.get("schedule_type")` 校验不受干扰。

- [ ] **Step 6: 运行确认通过**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/routes/schedules.py backend/tests/test_schedule_task_type.py
git commit -m "feat(schedule): REST 接受 task_type 并拒绝编辑改类型"
```

---

### Task 3: AI 工具 schedule_op 支持 task_type

**Files:**
- Modify: `backend/agent/tools/builtin/ops_schedule.py:212-260`（parameters）
- Modify: `backend/agent/tools/builtin/ops_schedule.py:302-314`（execute 签名与 create 转调）
- Modify: `backend/agent/tools/builtin/ops_schedule.py:317-318`（_create 签名）
- Modify: `backend/agent/tools/builtin/ops_schedule.py:372-396`（create data）
- Modify: `backend/agent/tools/builtin/ops_schedule.py`（_list 输出行）
- Test: `backend/tests/test_schedule_task_type.py`（追加）

**Interfaces:**
- Consumes: Task 1 `db.create_schedule` 读 task_type。
- Produces: `schedule_op(action="create", task_type="daily")` 入库带类型；非法值返回失败 ToolResult。

> **真实签名（勿再猜 kwargs）**：`execute(self, context, action, mode="interval", interval_minutes=None, cron=None, task=None, command=None, schedule_id=None, name=None, target_host=None)`，create 分支转调 `self._create(mode, interval_minutes, cron, task, command, name, target_host)`（:310）。`_create` 内变量：`task`→`task_prompt`、`name`→`task_name`。context 在 create 路径未使用。

- [ ] **Step 1: 追加失败测试**

```python
# spec 2026-10-27-schedule-task-type
import asyncio
import pytest
import database as db
from agent.tools.builtin.ops_schedule import ScheduleOpTool


@pytest.fixture
def tool_env(base_db, monkeypatch):
    # create 会调真引擎与 compute_next_run，挨一样挨着 test_schedule_routes 挨打补丁
    from test_schedule_routes import _FakeEngine, _fake_compute_next_run
    monkeypatch.setattr("agent.schedule_engine.get_schedule_engine", lambda: _FakeEngine())
    monkeypatch.setattr("agent.schedule_engine.compute_next_run", _fake_compute_next_run)
    return base_db


def test_tool_create_daily(tool_env):
    res = asyncio.run(ScheduleOpTool().execute(
        None, "create", mode="interval", interval_minutes=60,
        task="巡检", name="d", task_type="daily"))
    assert res.ok
    assert db.list_schedules()[-1]["task_type"] == "daily"


def test_tool_create_invalid_type_errors(tool_env):
    res = asyncio.run(ScheduleOpTool().execute(
        None, "create", mode="interval", interval_minutes=60,
        task="巡检", name="x", task_type="bogus"))
    assert not res.ok and "task_type" in res.error
```

> 首参 `None` 是 context（create 路径不用）。若 `_FakeEngine`/`_fake_compute_next_run` 不方便跨文件导入，在本测试文件内拷一份同实现。

- [ ] **Step 2: 运行确认失败**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: FAIL（未知参数被忽略 / 未报错）

- [ ] **Step 3: parameters 加 task_type**

`ops_schedule.py:226` `interval_minutes` 定义之前，`properties` 内加：

```python
            "task_type": {
                "type": "string",
                "enum": ["ops", "daily"],
                "default": "ops",
                "description": "任务分类：ops=运维任务，daily=日常任务（仅影响模板与列表分区，不改变执行）",
            },
```

- [ ] **Step 4: execute/_create 加形参 + 校验 + 入库**

`execute` 签名（:302-304）在 `target_host=None` 后加 `task_type="ops"`；create 转调（:310-311）改为：

```python
            return await self._create(mode, interval_minutes, cron, task, command, name,
                                      target_host, task_type)
```

`_create` 签名（:317-318）在 `target_host=None` 后加 `task_type="ops"`。`data`（:372）拼好后，在 `"enabled": 1,` 附近加：

```python
            "task_type": task_type,
```

校验：在 `data` 构造之前（:372 上方）加一段：

```python
        # spec 2026-10-27-schedule-task-type：分类字段非法值直接报错，不静默归一
        if (task_type or "ops") not in ("ops", "daily"):
            return ToolResult(False, "", f"非法 task_type: {task_type!r}，仅支持 ops / daily")
        task_type = task_type or "ops"
```

- [ ] **Step 5: list 输出附类型标注**

`_list` 渲染行（`ops_schedule.py:430`）在 `r.get('name')` 后插类型标注：

```python
            tag = "运维" if (r.get("task_type") or "ops") == "ops" else "日常"
            lines.append(f"{r.get('id')} | [{tag}] {r.get('name') or ''} | {st} | {first}")
```

- [ ] **Step 6: 运行确认通过**

Run: `npm run test:backend -- backend/tests/test_schedule_task_type.py`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/agent/tools/builtin/ops_schedule.py backend/tests/test_schedule_task_type.py
git commit -m "feat(schedule): schedule_op 支持 task_type 参数与列表标注"
```

---

### Task 4: 前端 API 类型与 SchedulesView 表单（标题区开关 + 模板切换）

**Files:**
- Modify: `src/api/agent.ts:124-142`（ScheduleRecord）、`:156-167`（ScheduleIn）
- Modify: `src/views/ssh/SchedulesView.vue:89-204`（弹窗模板）
- Modify: `src/views/ssh/SchedulesView.vue:263-362`（ScheduleForm/FORM_DEFAULTS/Template/templates）
- Modify: `src/views/ssh/SchedulesView.vue:476-510`（openEdit/formPayload）
- Test: `src/views/ssh/__tests__/SchedulesView.task-type.spec.ts`（新建）

**Interfaces:**
- Consumes: 后端 REST 已支持 task_type。
- Produces: 表单 `form.task_type`；create payload 带 task_type；编辑态类型锁定；模板按 task_type 过滤。

- [ ] **Step 1: ScheduleRecord/ScheduleIn 加字段**

`src/api/agent.ts:126` `name: string` 后加：

```ts
  task_type: 'ops' | 'daily'
```

`ScheduleIn`（:157 `name` 后）加：

```ts
  task_type?: 'ops' | 'daily'
```

- [ ] **Step 2: 写失败测试**

新建 `src/views/ssh/__tests__/SchedulesView.task-type.spec.ts`（沿用项目 Vitest + @vue/test-utils 挂载方式，参考现有 `src/**/__tests__/*.spec.ts`）：

```ts
// spec 2026-10-27-schedule-task-type
import { describe, it, expect } from 'vitest'
// 以项目现有 mount 辅助为准
import ScheduleFormExposer from '../SchedulesView.vue'

describe('定时任务类型', () => {
  it('新建默认 ops 且模板为运维集', async () => {
    const w = /* mount(SchedulesView, ...) 参考现有用例的 stub 方式 */ null
    // 断言 form.task_type === 'ops' 且模板 chips 含「定时巡检」
    expect(w.vm.form.task_type).toBe('ops')
    expect(w.text()).toContain('定时巡检')
  })
})
```

> 若 SchedulesView 未暴露 `form`，用 `<script setup>` 的 `defineExpose` 或改为断言 DOM：切到「日常」分段按钮后 chips 文本含「周报素材汇总」且不含「定时巡检」。优先 DOM 断言，避免暴露内部状态。

- [ ] **Step 3: 运行确认失败**

Run: `npm test -- SchedulesView.task-type`
Expected: FAIL（task_type 不存在）

- [ ] **Step 4: ScheduleForm 类型与默认值**

`SchedulesView.vue:264-275` `interface ScheduleForm` 首行加 `task_type: 'ops' | 'daily'`；`FORM_DEFAULTS`（:278）加 `task_type: 'ops',`。

- [ ] **Step 5: Template 加类型 + 日常模板**

`Template` 接口（:316-326）加 `task_type?: 'ops' | 'daily'`。现有 4 条模板各补 `task_type: 'ops'`。数组末尾追加 3 条日常模板：

```ts
  {
    id: 'daily-brief', name: '天气/资讯摘要提醒', task_type: 'daily',
    desc: '每天生成当天天气与要点摘要', schedule_type: 'daily', time_hour: 8,
    prompt: '汇总当天的天气、待办与要点信息，生成一条简洁的每日摘要提醒，面向个人日常使用。',
  },
  {
    id: 'weekly-report', name: '周报素材汇总', task_type: 'daily',
    desc: '每周五收集本周要点形成周报素材', schedule_type: 'weekly', weekday: 4, time_hour: 16,
    prompt: '收集本周完成的任务、进行中的事项与风险，整理成结构化周报素材草稿。',
  },
  {
    id: 'file-archive', name: '文件归档整理', task_type: 'daily',
    desc: '按类型/日期归类整理目录', schedule_type: 'monthly', day_of_month: 1, time_hour: 10,
    prompt: '对目标目录按文件类型与修改日期归类整理，先给出只读整理预览清单，确认后再执行。',
  },
```

新增 computed 供模板 chips 按类型过滤：

```ts
const visibleTemplates = computed(() =>
  templates.filter(t => (t.task_type ?? 'ops') === form.task_type))
```

模板列表渲染（:100 `v-for="t in templates"`）改为 `v-for="t in visibleTemplates"`。

- [ ] **Step 6: 标题栏放类型分段开关**

`SchedulesView.vue:93` `.modal-title` 改为 flex 容器并在右侧放开关：

```html
<div class="modal-title schedule-modal-title">
  <span>{{ editing.id ? '编辑任务' : '新建定时任务' }}</span>
  <div class="type-switch" :class="{ disabled: !!editing.id }">
    <button class="sched-option" :class="{ active: form.task_type === 'ops' }"
      :disabled="!!editing.id" @click="switchType('ops')">运维</button>
    <button class="sched-option" :class="{ active: form.task_type === 'daily' }"
      :disabled="!!editing.id" @click="switchType('daily')">日常</button>
  </div>
</div>
```

`switchType` 加到 script：

```ts
function switchType(t: 'ops' | 'daily') {
  if (editing.value?.id) return        // 编辑态锁定
  if (form.task_type === t) return
  form.task_type = t
  selectedTemplate.value = null          // 切类型清模板选中态，不重置已填字段
}
```

- [ ] **Step 7: openEdit 回填类型 + formPayload 带上**

`openEdit`（:478 起）加 `form.task_type = s.task_type ?? 'ops'`。`formPayload`（:498 return）加 `task_type: form.task_type,`。

> changedPayload 遍历 formPayload 键与 baseline 比对：编辑态 form.task_type 恒等于 baseline，不会发出 task_type，天然满足「PATCH 不改类型」。

- [ ] **Step 8: 样式**

`<style scoped>` 追加：

```css
.schedule-modal-title { display: flex; align-items: center; justify-content: space-between; }
.type-switch { display: inline-flex; gap: 6px; }
.type-switch .sched-option { padding: 3px 12px; font-size: 12px; }
.type-switch.disabled { opacity: .55; }
.type-switch .sched-option:disabled { cursor: not-allowed; }
```

- [ ] **Step 9: 运行确认通过 + 类型检查**

Run: `npm test -- SchedulesView.task-type` 然后 `npm run typecheck`
Expected: PASS 且无类型错误

- [ ] **Step 10: 提交**

```bash
git add src/api/agent.ts src/views/ssh/SchedulesView.vue src/views/ssh/__tests__/SchedulesView.task-type.spec.ts
git commit -m "feat(schedule): 弹窗标题区类型切换开关与模板按类型切换"
```

---

### Task 5: 列表分区 + 卡片色条区分

**Files:**
- Modify: `src/views/ssh/SchedulesView.vue:24-57`（列表渲染）
- Modify: `src/views/ssh/SchedulesView.vue:780-785`（卡片样式）
- Test: `src/views/ssh/__tests__/SchedulesView.task-type.spec.ts`（追加）

**Interfaces:**
- Consumes: Task 4 的 `form.task_type`、`ScheduleRecord.task_type`。
- Produces: 按 task_type 分组的 `opsItems`/`dailyItems` computed；空区隐藏；日常卡片 `.daily` 色条。

- [ ] **Step 1: 追加失败测试**

```ts
// spec 2026-10-27-schedule-task-type
it('按类型分区渲染，空区隐藏', () => {
  // stub scheduleApi.list 返回 1 条 ops + 1 条 daily
  // 断言出现两个分区标题「运维任务」「日常任务」，各含对应卡片
  // 再 stub 只有 ops：断言「日常任务」标题不出现
})
```

- [ ] **Step 2: 运行确认失败**

Run: `npm test -- SchedulesView.task-type`
Expected: FAIL（无分区结构）

- [ ] **Step 3: 分组 computed**

script 加：

```ts
const opsItems = computed(() => items.value.filter(s => (s.task_type ?? 'ops') === 'ops'))
const dailyItems = computed(() => items.value.filter(s => s.task_type === 'daily'))
```

- [ ] **Step 4: 列表改分区渲染**

将 `SchedulesView.vue:23-57` 的「empty-state + 单个 schedule-list」替换为两个区块。抽出卡片主体为可复用片段（用 `v-for` 两处各渲染，或抽 `<template>`）。空状态条件由 `items.length===0` 保持：

```html
<div v-if="items.length === 0 && !loading" class="empty-state">暂无定时任务</div>
<div v-if="items.length" class="schedule-list">
  <template v-if="opsItems.length">
    <div class="schedule-section-title">运维任务 <span class="cnt">{{ opsItems.length }}</span></div>
    <div v-for="s in opsItems" :key="s.id" class="schedule-card">
      <!-- 原卡片 head/meta/body 内容整体搬入，不变 -->
    </div>
  </template>
  <template v-if="dailyItems.length">
    <div class="schedule-section-title">日常任务 <span class="cnt">{{ dailyItems.length }}</span></div>
    <div v-for="s in dailyItems" :key="s.id" class="schedule-card daily">
      <!-- 同卡片内容 -->
    </div>
  </template>
</div>
```

> 卡片内部（head/meta/body/actions）与现有 `:25-56` 完全一致，仅外层从单 `v-for="s in items"` 变为两个分组各自 `v-for`。建议复制卡片块两次而非抽组件，避免大改；若重复过长可抽 `ScheduleCard.vue`，但属可选优化，非本任务强制。

- [ ] **Step 5: 样式（色条沿用 token + 分区标题）**

`.schedule-card` 已有 `border-left: 3px solid var(--accent-primary)`（运维天然成立）。追加：

```css
.schedule-card.daily { border-left-color: var(--accent-green); }
.schedule-section-title {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--text-primary); margin-top: 4px;
}
.schedule-section-title .cnt { font-size: 11px; font-weight: 400; color: var(--text-tertiary); }
```

> 色条仅冗余编码，分区标题「运维任务/日常任务」是主分类信息载体（无障碍兜底，符合 spec §3）。

- [ ] **Step 6: 运行确认通过 + 类型检查**

Run: `npm test -- SchedulesView.task-type` 然后 `npm run typecheck`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add src/views/ssh/SchedulesView.vue src/views/ssh/__tests__/SchedulesView.task-type.spec.ts
git commit -m "feat(schedule): 定时任务列表按类型分区并用色条区分"
```

---

## 收尾验证（全计划完成后跑一次）

- [ ] `npm run test:backend` 全绿（含新增 test_schedule_task_type.py）
- [ ] `npm test` 全绿（含 SchedulesView.task-type）
- [ ] `npm run typecheck` 无错误
- [ ] `npm run ci` 本地门禁通过
- [ ] 真机冒烟：新建运维/日常任务、编辑态类型置灰、列表两分区与色条、AI 工具创建带类型

## Self-Review 结论

- **Spec coverage**：§1 数据层→Task1；§1 REST→Task2；§4 工具→Task3；§2 弹窗/模板/编辑锁→Task4；§3 分区/色条→Task5；§5 错误处理→Task2/Task3；§6 测试→各 Task Step1。全部覆盖。
- **Placeholder scan**：测试 mount/fixture 处标注了「以项目现有用法为准」，属既有模式对齐而非 TBD，实现时按同目录参考文件落地。
- **Type consistency**：字段名统一 `task_type`、取值 `'ops'|'daily'`、computed `opsItems/dailyItems/visibleTemplates`、CSS `.daily` 全计划一致。
