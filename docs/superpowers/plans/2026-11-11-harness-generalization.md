# 通用 Harness 化（第一刀：身份通用化）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把产品默认身份从「运维智能体」改为「通用 harness 助手」，运维降级为四个内置专家之一（general / general_ops / research / copywriting），运维零回归。

**Architecture:** 身份随专家走（方案 A）。核心 prompt 只留领域中性文本；三块运维味文本（角色段 + 方法论两变体 + 运维工具纪律）全文搬进 `general_ops` 的 `identity_prompt`，经现有 `build_identity_section` 确定性注入（不做成技能——SkillRein 只注入目录+片段）。组装新序把专家身份段从末尾追加挪到基础身份之后。头less 路径（runbook / schedule）接同一套 `resolve_expert` / `build_identity_section`。前端加非阻塞「建议切运维专家」chip。一次 `_SCHEMA_VERSION` 升级完成 DB 迁移。

**Tech Stack:** Python 3 / FastAPI / SQLite（后端），Vue 3 / Pinia / TypeScript（前端），pytest / vitest / vue-tsc（测试），electron-vite（构建）。

**Spec:** `docs/superpowers/specs/2026-11-11-harness-generalization-design.md`（已获批，含 R1 审查修订）。本计划从 spec 论证，执行者两者都读。

## Global Constraints

- **唯一门禁**：`bash scripts/ci-check.sh`（后端 pytest → ruff F,E9 → vue-tsc → vitest → electron-vite build → hillgw selftest，整轮约 20 分钟）。开发期用靶向 pytest + typecheck/vitest；整刀完成后跑一次全量。**跑门禁期间不改树、套件独占机器**。
- **新守卫先证伪一次**（先红后绿）：每个新守卫写完先跑一次确认它真的会红（按计划给出的证伪方式临时改坏被测代码或断言），再恢复跑绿。
- **测试命名**：`backend/tests/` 文件名只写守什么（`test_<域>_<守什么>.py`），不写轮次/工单号。
- **README 双语铁律**：`README.md` / `README.en.md` 是同一份事实的两个语言投影，改功能/命令/架构必须同步另一侧。
- **示例脱敏**：公开面（README/注释/UI 占位符）只写占位符（`your-llm-host`、`example.com`）；真实内网地址/凭据只在 gitignored `backend/.env`。
- **提交只走显式路径**：`git add <具体文件>`，绝不 `git add -A`/`.`。当前工作树有大量**未跟踪的专家/插件地基 WIP**（`backend/agent/expert_policy.py`、`backend/routes/experts.py`、`backend/db/`、`src/components/settings/panels/ExpertsPanel.vue` 等）——见 Task 0，未经用户批准不得提交或改动这些文件之外的用户 WIP。
- **专家铁律**：只能收紧不能放宽；配置损坏 fail-closed 抛 `ValueError`，绝不把坏 JSON 当「继承」。
- **ruff 闸门**：只跑 F（未用导入/未定义名）+ E9（语法错），必须 0 命中。宽规则集存量告警是渐进整改项，不在本刀范围。
- **doc 引用口径**：docs 里 `文件:NNN` 引用须「已核实」；本计划行号为 2026-11-11 改动前锚点，实现后漂移以符号名为准。

## 关键设计决策（计划级，spec 的解释与收口）

1. **运维包 identity_prompt 的组成**（spec §4.2 三块 + 收口）：`SSH_MODE_PROMPT`（角色）+ 方法论选择规则 + `OPS_METHOD_READONLY` + `OPS_METHOD_CHANGE`（两变体全文）+ 运维工具纪律（工具名映射句 + log_cleanup allowlist）+ **探测命令规则** + **长任务规则**。后两者点名 `job_run(target='remote')`/`shell_exec` 超时等纯远程概念，研究/文案 run 的工具表里没有这些工具，留在核心会违反「不调不存在的工具」——故一并归运维包。核心非对话分支只留：通用工具纪律 + 记忆使用 + 任务拆分 + 子 Agent + 输出格式（皆领域中性）。
2. **中性基础身份** `BASE_IDENTITY_PROMPT` 由 `CHAT_MODE_PROMPT` 演化，去掉「本轮是纯对话」任务性措辞；纯对话引导仍由对话分支单独注入（现有字符串）。
3. **任务性质提示**通用化前置：`[任务性质] 只读 / 可能变更 / 未判定（按变更对待）`，仅非对话注入，放在身份层之前。`is_change=None→按变更` 的不对称取舍保留在提示文案 + 运维包选择规则里。核心不再注入 `OPS_METHOD_*`。
4. **研究/文案工具允许集**（spec §4.3「具体工具名以注册表为准，守卫按交集断言」）：`web_search, web_fetch, todo_write, load_skill, spawn_subagent, memory_search, memory_add, memory_invalidate, block_update, runbook_search`。不含 `shell_exec` / `ops_*` / `file_*` / `job_*` / `undo_restore` / `read_image`。`plugin_ids_json='[]'`（显式空集，杜绝 MCP 执行工具继承），`memory_scope='global'`。
5. **头less 专家接线单一来源**：`run_headless_once` 与 `schedule_engine` 各自调用同一套 `resolve_expert` / `build_identity_section`（不复制第二套逻辑）。schedule 构建器去 `SSH_MODE_PROMPT` 硬编码、改中性基础 + 接收身份段；runbook 显式传 `expert_slug="general_ops"`。专家 slug 查不到 → fail-closed 抛错（与交互路径 404 同口径）。
6. **建议事件是路由层直发**（与 `routes/agent.py` 初始 `state` 事件同模式，不经 harness TraceEvent）：登记进 `SSEAdapter._KIND_MAP` + 前端 type 联合；跨层守卫 `test_kind_map_covers_every_emitted_kind` 增加 `ROUTE_LEVEL_KINDS` 豁免集（诚实标注发射点在路由），保留其「抓死条目」本意。
7. **建议谓词是关键词级规则，不是新分类器**：复用 `intention._COMPILED_CHANGE` + 新增运维名词小词表，经 `_any_match_wordbounded` 匹配。触发 = 未选专家 + 非纯对话 +（变更动词 ∨ 运维名词）。判定失败静默 no-op。
8. **「切换并重跑」**：建议事件携带 `task` 原文；前端 accept = 按 slug 找专家 → `setSelectedExpert` → `agent.abort()` → `sshAi.sendMessage(task)` 重发（中止的旧尝试留痕为「已中止」，重跑是带专家的新回合）。

## 文件结构

| 文件 | 责任 | 动作 |
| --- | --- | --- |
| `backend/prompts.py` | 全部 prompt 文本常量单一来源 | 改：加 `BASE_IDENTITY_PROMPT`/`GENERAL_TOOL_DISCIPLINE`/`OPS_TOOL_DISCIPLINE`/`OPS_PROBE_RULES`/`OPS_LONGTASK_RULES`/`OPS_IDENTITY_PROMPT`/`RESEARCH_IDENTITY_PROMPT`/`COPYWRITING_IDENTITY_PROMPT` |
| `backend/agent/expert_policy.py` | 专家策略纯函数 + 内置工厂 | 改：`DEFAULT_EXPERT_SLUG→general`；加 `builtin_general/research/copywriting`；`builtin_general_ops` 改运维包 |
| `backend/routes/agent.py` | 交互路径 prompt 组装 + 建议发射 | 改：`_build_agent_system_prompt` 新序 + `identity_section` 参；删末尾身份追加；加建议 yield |
| `backend/agent/expert_suggest.py` | 建议谓词纯函数 | 新建 |
| `backend/database.py` | DB 迁移 | 改：`_SCHEMA_VERSION 9→10`；四内置种子；general_ops 升级；schedules expert_slug 列 + 回填 |
| `backend/agent/headless_run.py` | 无头 run 装配 | 改：加 `expert_slug` 参 + 专家解析 + 身份拼接 |
| `backend/agent/schedule_engine.py` | 定时任务装配 | 改：构建器去硬编码 + 身份段；run 方法解析专家 |
| `backend/runbook_engine.py` | 剧本无头 run | 改：两处调用传 `expert_slug="general_ops"` |
| `backend/routes/schedules.py` | 定时任务路由 | 改：`ScheduleIn`/`SchedulePatch` 加 `expert_slug` |
| `backend/agent/harness/telemetry.py` | SSE 投影 | 改：`_KIND_MAP` 加 `expert_suggestion` |
| `src/api/sse.ts` | 前端 SSE 类型 | 改：type 联合 + `slug`/`name` 字段 |
| `src/stores/agent.ts` | run 执行态 | 改：`expertSuggestion` 态 + case + dismiss |
| `src/components/home/HomeComposer.vue` | 输入区 | 改：建议 chip + accept/ignore |
| `README.md` / `README.en.md` / `CONTEXT.md` | 文档 | 改：通用身份 + 四专家 + 术语 |

---

### Task 0: 基线检查点（用户决策门）

**Files:** 无代码改动。

**Interfaces:**
- Consumes: 当前工作树状态。
- Produces: 一个干净的、地基已提交的基线，后续任务才能安全改动 `expert_policy.py` 等文件。

**背景：** 专家/插件地基（`expert_policy.py`、`routes/experts.py`、`db/experts.py`、`ExpertsPanel.vue`、`src/stores/experts.ts` 等）当前是**未跟踪 WIP**，另有约 60 个已修改文件。本刀要改这些文件，必须先把地基落盘，否则 diff 与回滚都无从谈起。

- [ ] **Step 1: 向用户呈现 WIP 状态并请求决策**

跑 `git status` 与 `git log --oneline -3`，把未跟踪的专家/插件地基文件清单呈现给用户，问：

> 专家/插件地基目前是未提交 WIP。本刀会改 `expert_policy.py` 等文件。是否先把地基按显式路径提交一个基线 commit？（推荐：是。否则本刀的改动会和地基 WIP 混在同一份未跟踪 diff 里，无法单独回滚。）

**这是用户决策门：未获批准不得提交用户 WIP，也不得开始 Task 1。** 若用户选择「先提交地基」，按显式路径 `git add` 地基文件（逐个核对，绝不 `git add -A`）并 commit；若用户选择「在地基上直接改」，记录该决定并继续，但后续每个任务的 commit 仍只走本任务显式路径。

- [ ] **Step 2: 验证基线绿**

Run: `cd backend && python -m pytest tests/test_expert_policy_legacy_noop.py tests/test_expert_policy_safety_intersection.py tests/test_agent_system_prompt.py tests/test_expert_routes_crud.py -q`
Expected: 全 PASS（这是改动前的存量守卫，必须绿）。

- [ ] **Step 3: 记录基线**

把基线 commit hash（或「用户批准在地基 WIP 上直接改」）记入执行日志。无需 commit（本任务无代码改动）。

---

### Task 1: 专家包配置层（四内置工厂 + 运维包文本）

**Files:**
- Modify: `backend/prompts.py`（加运维/领域身份常量）
- Modify: `backend/agent/expert_policy.py:29-30`（DEFAULT slug/name）、`:87-104`（builtin_general_ops）、新增工厂
- Modify: `backend/tests/test_expert_policy_legacy_noop.py`（红线基准 general_ops→general）
- Modify: `backend/tests/test_expert_policy_safety_intersection.py:24-28`（make_expert 基底换 builtin_general）
- Test: `backend/tests/test_expert_builtin_packs.py`（新建）

**Interfaces:**
- Consumes: 现有 `resolve_expert` / `build_identity_section` / `filter_tool_names`（不改签名）。
- Produces:
  - `prompts.OPS_IDENTITY_PROMPT: str`、`RESEARCH_IDENTITY_PROMPT: str`、`COPYWRITING_IDENTITY_PROMPT: str`
  - `expert_policy.DEFAULT_EXPERT_SLUG == "general"`、`DEFAULT_EXPERT_NAME == "通用"`
  - `expert_policy.builtin_general() -> dict`、`builtin_general_ops() -> dict`、`builtin_research() -> dict`、`builtin_copywriting() -> dict`（返回与现有 `builtin_general_ops` 同键集的 DB 种子行 dict）
  - `expert_policy._DOMAIN_READONLY_TOOLS: tuple[str, ...]`

- [ ] **Step 1: 写新守卫（先红）**

Create `backend/tests/test_expert_builtin_packs.py`:

```python
"""四个内置专家包的配置契约：运维包语义等价 / 领域包收紧 / general 全继承 / 尺寸自律。

红线基准从 general_ops 迁到 general（spec §8）：未选专家 ≡ general。
general_ops 不再是「等价于无专家」，而是真运维包——身份段含角色 + 方法论两变体 + 工具纪律。
"""
import json

import pytest

from agent.expert_policy import (
    DEFAULT_EXPERT_SLUG,
    _DOMAIN_READONLY_TOOLS,
    builtin_copywriting,
    builtin_general,
    builtin_general_ops,
    builtin_research,
    build_identity_section,
    filter_tool_names,
    resolve_expert,
)
from prompts import (
    OPS_METHOD_CHANGE,
    OPS_METHOD_READONLY,
    SSH_MODE_PROMPT,
)

# 真实注册表里的执行/远程文件工具（守卫按交集断言，spec §4.3）
EXECUTION_TOOLS = [
    "shell_exec", "file_read", "file_write", "file_edit", "file_glob", "file_grep",
    "sys_overview", "process_inspect", "network_probe", "log_read",
    "fleet_sessions", "fleet_overview", "service_manage", "schedule_op", "log_cleanup",
    "job_run", "job_list", "job_output", "job_kill", "undo_restore", "read_image",
]


def test_default_slug_is_general():
    assert DEFAULT_EXPERT_SLUG == "general"


def test_general_is_fully_inherited():
    policy = resolve_expert(builtin_general())
    assert policy.is_legacy
    assert policy.expert_slug == "general"
    assert build_identity_section(policy) == ""


def test_ops_pack_identity_contains_role_and_both_methods_and_discipline():
    """运维包语义等价：角色段关键句 + 方法论两变体全文 + 运维工具纪律齐全。
    证伪：删掉 OPS_IDENTITY_PROMPT 里任一变体 → 红。"""
    policy = resolve_expert(builtin_general_ops())
    section = build_identity_section(policy)
    assert "智能运维助手" in section            # 角色段关键句
    assert OPS_METHOD_READONLY in section       # 只读变体全文
    assert OPS_METHOD_CHANGE in section         # 变更变体全文
    assert "纯只读" in section                  # CHANGE 第 0 条自行降级
    assert "log_cleanup" in section             # 运维工具纪律
    assert "file_read" in section               # 工具名映射句
    assert "job_run" in section                 # 长任务规则
    assert "[任务性质]" in section              # 选择规则引用任务性质提示


def test_ops_pack_tools_and_permission_fully_inherited():
    policy = resolve_expert(builtin_general_ops())
    assert policy.tool_names is None
    assert policy.plugin_ids is None
    assert policy.permission_ceiling == "inherit"
    assert policy.memory_scope == "inherit"
    assert policy.max_iterations is None


@pytest.mark.parametrize("factory", [builtin_research, builtin_copywriting])
def test_domain_pack_has_no_execution_tools(factory):
    """领域包收紧：工具集 ∩ 执行工具 = ∅。证伪：给文案包加 shell_exec → 红。"""
    policy = resolve_expert(factory())
    assert policy.tool_names is not None
    assert not (set(policy.tool_names) & set(EXECUTION_TOOLS))
    # 与真实可用集求交后仍不含执行工具
    available = EXECUTION_TOOLS + list(_DOMAIN_READONLY_TOOLS)
    assert not (set(filter_tool_names(available, policy.tool_names)) & set(EXECUTION_TOOLS))


@pytest.mark.parametrize("factory", [builtin_research, builtin_copywriting])
def test_domain_pack_plugins_explicit_empty_and_memory_global(factory):
    """plugin_ids=() 显式空集（不是 None 继承）；memory_scope=global。
    证伪：置 plugin_ids=None → 红。"""
    policy = resolve_expert(factory())
    assert policy.plugin_ids == ()
    assert policy.memory_scope == "global"
    assert json.loads(factory()["plugin_ids_json"]) == []


@pytest.mark.parametrize("factory", [builtin_general, builtin_general_ops, builtin_research, builtin_copywriting])
def test_builtin_seed_enabled_and_marked(factory):
    seed = factory()
    assert seed["enabled"] == 1
    assert seed["is_builtin"] == 1
    assert seed["revision"] == 1


def test_ops_identity_within_api_limit():
    """内置种子虽走直连 SQL，仍按 API 上限 8000 自律（spec §4.2）。"""
    assert len(builtin_general_ops()["identity_prompt"]) < 8000
```

- [ ] **Step 2: 跑新守卫确认红**

Run: `cd backend && python -m pytest tests/test_expert_builtin_packs.py -q`
Expected: FAIL（`ImportError: cannot import name '_DOMAIN_READONLY_TOOLS'` / `builtin_general` 等不存在）。

- [ ] **Step 3: 在 prompts.py 加运维包文本常量**

在 `backend/prompts.py` 的 `OPS_METHOD_CHANGE` 之后、`_current_time_str` 之前追加（工具纪律/探测/长任务三段从 `routes/agent.py` 现有内联字符串逐字提取）：

```python
# ── 运维专家包身份文本（spec §4.2：三块运维味文本 + 远程专属纪律，全文进 identity_prompt）──
# 这些常量只被 expert_policy.builtin_general_ops() 消费；核心组装器不再注入它们。

OPS_TOOL_DISCIPLINE = (
    "运维工具映射：读文件用 file_read、改文件用 file_edit、搜内容用 file_grep、"
    "找文件用 file_glob、看日志用 log_read、查进程用 process_inspect、看资源用 sys_overview、"
    "探端口用 network_probe、管服务用 service_manage。"
    "清理日志用 log_cleanup（默认 dry_run 只列出匹配文件，把清单给用户确认后"
    "再用 dry_run=false 删除），不要用 find -delete / rm -rf 手工拼删除命令；"
    "生产环境删除需预先在 allowlist 登记 log_cleanup，但试运行预览无需登记。"
    "若目标目录不在 log_cleanup 允许范围（/tmp、/var/tmp、/var/log 应用子目录、"
    "agent 托管日志目录）内，直接告知用户该目录需人工清理并给出命令，"
    "不要构造删除命令的变体。"
)

OPS_PROBE_RULES = (
    "探测命令规则：一次执行多段探测/排查命令时用 ; 串联各段，不要用 &&——"
    "du/find/ls 遇到无权限路径会以非零码退出（属预期），&& 链会因此中断后续段落。"
    "命令 exit code 非零不等于连接或执行异常：先看已捕获的输出内容再下结论"
    "（du 仍会输出可读部分的大小）。"
    "注意：末段 `echo ...:$?` 捕获的是整行最后一条命令的退出码，多段探测时"
    "末段若因权限报错会把整行标成失败。对以探测/统计为主的命令链，建议在结尾追加"
    "`; true` 使整体退出码收敛为 0，避免把可读的探测结果误报为失败；对真正需要"
    "判断成败的关键命令则不要追加。"
)

OPS_LONGTASK_RULES = (
    "长任务规则：预计执行超过 60 秒的任务（大文件传输、构建、批量巡检、等待类操作）"
    "必须用 job_run(target='remote') 后台启动，再用 job_output(job_id, wait=10~30) "
    "轮询输出，需要停止时用 job_kill。不要用 shell_exec 同步等待（超时上限 120 秒）。"
)

OPS_METHOD_SELECTION = (
    "运维方法选择：根据上方 [任务性质] 提示选择——只读任务用『远程运维方法（只读任务）』；"
    "可能变更或未判定（按变更对待）用『远程运维方法（变更任务）』。"
    "变更方法第 0 条允许你在探测后判定本任务实际纯只读时自行降级。"
)

# 运维包完整身份：角色 + 选择规则 + 方法论两变体全文 + 运维工具纪律 + 远程专属纪律。
OPS_IDENTITY_PROMPT = "\n\n".join([
    SSH_MODE_PROMPT,
    OPS_METHOD_SELECTION,
    OPS_METHOD_READONLY,
    OPS_METHOD_CHANGE,
    OPS_TOOL_DISCIPLINE,
    OPS_PROBE_RULES,
    OPS_LONGTASK_RULES,
])

RESEARCH_IDENTITY_PROMPT = (
    "你是研究助手，擅长信息检索、资料分析与综述。基于检索到的真实资料作答，"
    "标明信息来源与不确定性；不要编造事实或来源。输出结构化中文报告："
    "先给结论，再分节展开证据与推理。"
)

COPYWRITING_IDENTITY_PROMPT = (
    "你是文案助手，擅长撰写与润色各类文案。按用户给定的受众、语气与长度要求创作；"
    "事实性内容需基于可靠资料，不夸大不编造。直接交付文案成品，必要时附简短说明。"
)
```

- [ ] **Step 4: 改 expert_policy.py 的 DEFAULT 与工厂**

修改 `backend/agent/expert_policy.py`：

把 `:29-30` 改为：
```python
DEFAULT_EXPERT_SLUG = "general"
DEFAULT_EXPERT_NAME = "通用"
```

在模块顶部 import 区加（prompts 是纯叶子模块，无环）：
```python
from prompts import (
    COPYWRITING_IDENTITY_PROMPT,
    OPS_IDENTITY_PROMPT,
    RESEARCH_IDENTITY_PROMPT,
)
```

把现有 `builtin_general_ops()`（`:87-104`）整段替换为下面的工厂组：
```python
# 研究/文案包共享的「无执行」工具允许集：联网检索 + 待办 + 技能 + 子Agent + 记忆 + 经验检索。
# 不含 shell_exec / ops_* / file_* / job_* / undo_restore / read_image —— 执行工具物理缺席。
_DOMAIN_READONLY_TOOLS = (
    "web_search", "web_fetch",
    "todo_write", "load_skill", "spawn_subagent",
    "memory_search", "memory_add", "memory_invalidate", "block_update",
    "runbook_search",
)


def _builtin_base(slug: str, name: str, description: str) -> dict:
    """内置种子行骨架：全继承 + 启用 + builtin 标记。各领域包按需收紧。"""
    return {
        "slug": slug,
        "name": name,
        "description": description,
        "identity_prompt": "",
        "model_id": None,
        "tool_names_json": None,
        "plugin_ids_json": None,
        "skill_ids_json": None,
        "permission_ceiling": "inherit",
        "max_iterations": None,
        "memory_scope": "inherit",
        "enabled": 1,
        "is_builtin": 1,
        "revision": 1,
    }


def builtin_general() -> dict:
    """内置默认专家「通用」：全继承，选中它等价于没有专家（新红线基准）。"""
    return _builtin_base(
        DEFAULT_EXPERT_SLUG, DEFAULT_EXPERT_NAME,
        "通用助手（默认）：模型、工具、技能、权限与记忆全部继承全局设置。",
    )


def builtin_general_ops() -> dict:
    """内置运维专家：运维角色 + 方法论两变体 + 运维工具纪律全文进身份段；工具/权限全继承。"""
    row = _builtin_base(
        "general_ops", "运维专家",
        "Linux 运维专家：运维角色 + 变更/只读方法论 + 运维工具纪律；工具与权限全继承。",
    )
    row["identity_prompt"] = OPS_IDENTITY_PROMPT
    return row


def builtin_research() -> dict:
    """内置研究专家：收紧到无执行工具集，插件显式空集，记忆限 global。"""
    row = _builtin_base(
        "research", "研究",
        "研究员：联网检索 + 记忆 + 待办，无执行工具与插件，记忆限 global。",
    )
    row["identity_prompt"] = RESEARCH_IDENTITY_PROMPT
    row["tool_names_json"] = json.dumps(list(_DOMAIN_READONLY_TOOLS))
    row["plugin_ids_json"] = "[]"
    row["memory_scope"] = "global"
    return row


def builtin_copywriting() -> dict:
    """内置文案专家：收紧到无执行工具集，插件显式空集，记忆限 global。"""
    row = _builtin_base(
        "copywriting", "文案",
        "文案助手：联网检索 + 记忆 + 待办，无执行工具与插件，记忆限 global。",
    )
    row["identity_prompt"] = COPYWRITING_IDENTITY_PROMPT
    row["tool_names_json"] = json.dumps(list(_DOMAIN_READONLY_TOOLS))
    row["plugin_ids_json"] = "[]"
    row["memory_scope"] = "global"
    return row
```

更新模块 docstring 里「`general_ops`（通用运维）是全继承的内置专家」一段为「`general`（通用）是全继承的内置专家：未选专家与选中它完全一致；`general_ops` 是真运维包（身份段含运维角色与方法论）。这条由 test_expert_policy_legacy_noop.py 与 test_expert_builtin_packs.py 钉住。」

- [ ] **Step 5: 迁移存量红线测试（legacy_noop）**

修改 `backend/tests/test_expert_policy_legacy_noop.py`：红线基准从 `builtin_general_ops` 换 `builtin_general`。
- import 区把 `builtin_general_ops` 改为 `builtin_general`。
- `test_general_ops_matches_none_on_every_runtime_dimension` 改名 `test_general_matches_none_on_every_runtime_dimension`，体内 `builtin_general_ops()` → `builtin_general()`，`assert default.expert_slug == DEFAULT_EXPERT_SLUG` 保留（现 == "general"）。
- `test_legacy_identity_section_is_empty_string`：`builtin_general_ops()` → `builtin_general()`（两处）。
- `test_general_ops_snapshot_differs_only_in_attribution` 改名 `test_general_snapshot_differs_only_in_attribution`，体内 `builtin_general_ops()` → `builtin_general()`。
- `test_builtin_seed_is_enabled_and_readonly_marked`：`builtin_general_ops()` → `builtin_general()`（`plugin_ids_json is None` 仍成立）。
- 文件 docstring 首行「未选专家 / 选中 general_ops」改为「未选专家 / 选中 general」。

- [ ] **Step 6: 迁移安全求交测试基底（safety_intersection）**

修改 `backend/tests/test_expert_policy_safety_intersection.py`：
- import 区 `builtin_general_ops` → `builtin_general`。
- `make_expert`（`:24-28`）体内 `row = dict(builtin_general_ops())` → `row = dict(builtin_general())`（全继承中性基底；general_ops 现在带身份，不适合当自定义专家夹具基底）。

- [ ] **Step 7: 跑全部相关测试确认绿**

Run: `cd backend && python -m pytest tests/test_expert_builtin_packs.py tests/test_expert_policy_legacy_noop.py tests/test_expert_policy_safety_intersection.py -q`
Expected: 全 PASS。

- [ ] **Step 8: 证伪新守卫一次（先红后绿）**

临时把 `prompts.py` 的 `OPS_IDENTITY_PROMPT` 组装里的 `OPS_METHOD_READONLY,` 一行删掉，跑 `cd backend && python -m pytest tests/test_expert_builtin_packs.py::test_ops_pack_identity_contains_role_and_both_methods_and_discipline -q` → 必须 FAIL；恢复该行再跑 → PASS。再临时把 `builtin_copywriting` 的 `row["plugin_ids_json"] = "[]"` 改为 `None`，跑 `test_domain_pack_plugins_explicit_empty_and_memory_global` → FAIL；恢复 → PASS。

- [ ] **Step 9: ruff + commit**

Run: `cd backend && python -m ruff check --select F,E9 prompts.py agent/expert_policy.py tests/test_expert_builtin_packs.py`
Expected: 0 命中。

```bash
git add backend/prompts.py backend/agent/expert_policy.py backend/tests/test_expert_builtin_packs.py backend/tests/test_expert_policy_legacy_noop.py backend/tests/test_expert_policy_safety_intersection.py
git commit -m "feat(expert): 四内置专家包配置层，默认身份 general，运维包全文身份段"
```

---

### Task 2: 核心 prompt 中性化 + 组装新序

**Files:**
- Modify: `backend/prompts.py`（加 `BASE_IDENTITY_PROMPT` / `GENERAL_TOOL_DISCIPLINE`）
- Modify: `backend/routes/agent.py:184-314`（`_build_agent_system_prompt`）、`:983-994`（删末尾身份追加，改传参）
- Test: `backend/tests/test_agent_system_prompt.py`（按新序重写）

**Interfaces:**
- Consumes: Task 1 的 `OPS_IDENTITY_PROMPT`、`builtin_general_ops`、`build_identity_section`。
- Produces:
  - `prompts.BASE_IDENTITY_PROMPT: str`、`GENERAL_TOOL_DISCIPLINE: str`
  - `_build_agent_system_prompt(req, profile_text, is_conversation_task, is_change=None, goal_text="", identity_section="") -> str`（新增末位关键字参 `identity_section`）

- [ ] **Step 1: 重写组装守卫（先红）**

把 `backend/tests/test_agent_system_prompt.py` 整份替换为：

```python
"""Agent system prompt 组装测试（routes/agent.py::_build_agent_system_prompt）。

通用 harness 化后的新契约（spec §5/§8）：
  - 核心中性：general（identity_section=""）下不含运维身份/方法论/运维工具纪律；
  - 组装新序：时间 → 任务性质提示（仅非对话）→ 中性基础身份 → 专家身份段 → 姿态 → goal → 画像 → 用户 prompt → 分支引导；
  - 专家身份段紧随基础身份、先于 goal/画像（不再末尾追加）；
  - 任务性质提示：非对话含且先于身份段，None→按变更，对话分支不含；
  - 运维语义等价：传入运维身份段时角色 + 方法论两变体 + 工具纪律齐全；
  - 通用引导（记忆/拆分/子Agent/输出格式）不得被挤掉。
"""
import inspect

import pytest

from agent.expert_policy import build_identity_section, builtin_general_ops, resolve_expert
from prompts import BASE_IDENTITY_PROMPT, OPS_METHOD_CHANGE, OPS_METHOD_READONLY
from routes.agent import AgentRunRequest, _build_agent_system_prompt

PROFILE = "[目标机环境] 发行版: Ubuntu 22.04.3 LTS; init: systemd; 包管理器: apt-get"
OPS_SECTION = build_identity_section(resolve_expert(builtin_general_ops()))


def _req(**kw) -> AgentRunRequest:
    base = dict(task="重启 nginx", conversation_id="c1")
    base.update(kw)
    return AgentRunRequest(**base)


# ── 核心中性（general 策略）─────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", [
    "智能运维助手", "远程运维方法", "log_cleanup", "读文件用 file_read",
])
def test_core_prompt_is_domain_neutral(fragment):
    """证伪：把 SSH_MODE_PROMPT 注回核心 → 红。"""
    prompt = _build_agent_system_prompt(_req(), PROFILE, False, is_change=True)
    assert fragment not in prompt


def test_core_uses_neutral_base_identity():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert BASE_IDENTITY_PROMPT in prompt


# ── 任务性质提示 ───────────────────────────────────────────────────────────

def test_task_nature_hint_readonly():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=False)
    assert "[任务性质] 只读" in prompt


def test_task_nature_hint_change():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    assert "[任务性质] 可能变更" in prompt


def test_task_nature_hint_unknown_defaults_to_change():
    """证伪：删掉 None 分支 → 红。"""
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "[任务性质] 未判定（按变更对待）" in prompt


def test_task_nature_hint_before_identity_and_absent_in_conversation():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True, identity_section=OPS_SECTION)
    assert prompt.index("[任务性质]") < prompt.index(OPS_SECTION)
    conv = _build_agent_system_prompt(_req(task="你好"), "", True)
    assert "[任务性质]" not in conv


# ── 专家身份段位置 ─────────────────────────────────────────────────────────

def test_identity_section_right_after_base_before_goal_and_profile():
    """证伪：挪回末尾追加 → 红。"""
    prompt = _build_agent_system_prompt(
        _req(), PROFILE, False, is_change=True,
        goal_text="[当前目标] 梳理索引", identity_section=OPS_SECTION,
    )
    assert prompt.index(BASE_IDENTITY_PROMPT) < prompt.index(OPS_SECTION)
    assert prompt.index(OPS_SECTION) < prompt.index("[当前目标]")
    assert prompt.index(OPS_SECTION) < prompt.index(PROFILE)


def test_empty_identity_section_adds_no_bytes():
    with_empty = _build_agent_system_prompt(_req(), "", False, identity_section="")
    without = _build_agent_system_prompt(_req(), "", False)
    assert with_empty == without


# ── 运维语义等价（传入运维身份段）──────────────────────────────────────────

def test_ops_section_carries_role_and_both_methods():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True, identity_section=OPS_SECTION)
    assert "智能运维助手" in prompt
    assert OPS_METHOD_READONLY in prompt
    assert OPS_METHOD_CHANGE in prompt
    assert "纯只读" in prompt


# ── 画像注入 ───────────────────────────────────────────────────────────────

def test_host_profile_injected():
    assert PROFILE in _build_agent_system_prompt(_req(), PROFILE, False)


def test_host_profile_before_general_tool_guidance():
    prompt = _build_agent_system_prompt(_req(), PROFILE, False)
    assert prompt.index(PROFILE) < prompt.index("工具使用效率")


def test_host_profile_omitted_when_empty():
    assert "[目标机环境]" not in _build_agent_system_prompt(_req(), "", False)


def test_host_profile_not_injected_for_conversation():
    prompt = _build_agent_system_prompt(_req(task="你好"), PROFILE, True)
    assert PROFILE not in prompt


# ── 对话分支 ───────────────────────────────────────────────────────────────

def test_conversation_skips_tools_and_uses_neutral_identity():
    prompt = _build_agent_system_prompt(_req(task="你好"), "", True, is_change=False)
    assert "智能运维助手" not in prompt
    assert "工具使用效率" not in prompt
    assert "子 Agent" not in prompt
    assert "纯对话型问题" in prompt
    assert BASE_IDENTITY_PROMPT in prompt


# ── 通用引导回归守护 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", ["工具使用效率", "记忆使用", "任务拆分", "子 Agent", "输出格式"])
def test_general_guidance_preserved(fragment):
    assert fragment in _build_agent_system_prompt(_req(), PROFILE, False, is_change=True)


def test_memory_tool_guidance_names_tools():
    prompt = _build_agent_system_prompt(_req(), "", False, is_change=True)
    for name in ("memory_search", "memory_add", "block_update", "memory_invalidate"):
        assert name in prompt


def test_time_injection_present():
    assert "[系统时间]" in _build_agent_system_prompt(_req(), "", False)


def test_user_system_prompt_appended_and_no_backdoor_params():
    prompt = _build_agent_system_prompt(_req(system_prompt="[自定义] 严格模式"), PROFILE, False)
    assert "[自定义] 严格模式" in prompt
    params = inspect.signature(_build_agent_system_prompt).parameters
    assert "reflection_injection" not in params
    assert "host_profile_text" not in params


# ── 对话姿态（workflow_mode）──────────────────────────────────────────────

def test_goal_posture_injects_goal_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="goal"), "", False)
    assert "「目标」姿态" in prompt
    assert "「计划」姿态" not in prompt


def test_plan_posture_injects_plan_prompt():
    prompt = _build_agent_system_prompt(_req(workflow_mode="plan"), "", False)
    assert "「计划」姿态" in prompt
    assert "「目标」姿态" not in prompt


def test_absent_posture_injects_neither():
    prompt = _build_agent_system_prompt(_req(), "", False)
    assert "「目标」姿态" not in prompt
    assert "「计划」姿态" not in prompt


def test_workflow_mode_field_accepts_null_and_rejects_unknown():
    from pydantic import ValidationError
    assert _req(workflow_mode=None).workflow_mode is None
    for junk in ("Goal", "goals", "any"):
        with pytest.raises(ValidationError):
            _req(workflow_mode=junk)
```

- [ ] **Step 2: 跑守卫确认红**

Run: `cd backend && python -m pytest tests/test_agent_system_prompt.py -q`
Expected: FAIL（`ImportError: cannot import name 'BASE_IDENTITY_PROMPT'`；中性/位置断言失败）。

- [ ] **Step 3: 在 prompts.py 加中性基础身份与通用工具纪律**

在 `backend/prompts.py` 的 `CHAT_MODE_PROMPT` 之后追加（`CHAT_MODE_PROMPT` 保留——仍被 schedule 旧路径与潜在引用使用，Task 4 再收口；核心组装器改用 `BASE_IDENTITY_PROMPT`）：

```python
# 通用 harness 的中性基础身份（spec §4.1）：由 CHAT_MODE_PROMPT 演化，去掉「本轮是纯对话」
# 任务性措辞——基础身份不得自称纯对话。纯对话引导仍由对话分支单独注入。
BASE_IDENTITY_PROMPT = (
    "你是 cKunAgentSSH 内置的通用 AI 助手，可协助多种领域的任务。请用中文回答；"
    "若需要给出命令或配置示例，用 ```bash 代码块包裹并说明作用与风险。"
    "如实汇报结果，不要声称未完成的工作已完成；不要调用工具列表中不存在的工具。"
)

# 核心保留的工具无关纪律（spec §4.2）：运维味段落（工具名映射/log_cleanup/探测/长任务）
# 已随 OPS_IDENTITY_PROMPT 进运维包，这里只留跨领域通用部分。
GENERAL_TOOL_DISCIPLINE = (
    "工具使用效率：优先使用专用工具而非通用 shell 拼命令；基于返回内容直接推理，"
    "避免短时间内重复读取同一内容；查看大文件的不同位置时优先指定 offset/limit 读取相关片段。"
    "不要调用工具列表中不存在的工具。"
)
```

- [ ] **Step 4: 重写 _build_agent_system_prompt**

把 `backend/routes/agent.py:184-314` 的函数体替换为（签名加 `identity_section`，新序，删运维内联段）：

```python
def _build_agent_system_prompt(
    req: "AgentRunRequest",
    profile_text: str,
    is_conversation_task: bool,
    is_change: Optional[bool] = None,
    goal_text: str = "",
    identity_section: str = "",
) -> str:
    """构建 Agent 系统提示（通用 harness 新序，spec §5）

    拼装顺序：时间 → 任务性质提示（仅非对话）→ 中性基础身份 → 专家身份段
              → workflow 姿态 → goal → 画像块 → 用户 system_prompt
              → [对话引导 | 通用工具纪律 + 记忆/拆分/子Agent/输出格式]

    identity_section: 专家身份段（build_identity_section(policy) 的结果），空串不追加任何字节。
        紧随基础身份注入（身份层放最靠前以获得最高注意力），不再末尾追加。
    is_change: 任务是否可能改变状态。None=未做意图识别，提示文案按「未判定（按变更对待）」，
        不对称取舍保留（运维包选择规则消费该提示）。
    """
    from prompts import BASE_IDENTITY_PROMPT, GENERAL_TOOL_DISCIPLINE

    time_injection = (
        f"[系统时间] 当前时间：{_current_time_str()}。"
        f"如用户询问日期/星期/时间，请以本信息为准，不要使用训练数据中的旧时间。"
    )
    parts = [time_injection]

    # 任务性质提示：通用元数据，前置在身份层之前；运维身份段的选择规则引用它。
    if not is_conversation_task:
        if is_change is False:
            nature = "只读"
        elif is_change is True:
            nature = "可能变更"
        else:
            nature = "未判定（按变更对待）"
        parts.append(f"[任务性质] {nature}")

    # 中性基础身份（领域无关，恒注入）+ 专家身份段（空串不加字节）
    parts.append(BASE_IDENTITY_PROMPT)
    if identity_section:
        parts.append(identity_section)

    # workflow 姿态：仅非对话 + 用户显式表态（None=未表态不注入，× 才能真关掉）
    if not is_conversation_task and req.workflow_mode is not None:
        parts.append(_WORKFLOW_MODE_PROMPTS[req.workflow_mode])
    if goal_text:
        parts.append(goal_text)
    if profile_text and not is_conversation_task:
        parts.append(profile_text)
    if req.system_prompt:
        parts.append(req.system_prompt)

    if is_conversation_task:
        parts.append(
            "本任务为纯对话型问题，请直接简洁回答，不要调用任何工具，不要做长篇规划。"
            "回答应基于你自身的身份和能力，避免引用无关上下文或记忆。"
        )
    else:
        parts.append(GENERAL_TOOL_DISCIPLINE)
        parts.append(
            "记忆使用：上下文里 [画像] 是常驻已知事实、[相关记忆] 是本轮检索到的历史经验，"
            "可直接引用无需再调工具。需要更多历史经验、或判断某做法在其他主机上的先例时用 "
            "memory_search；确认了值得长期保留的主机特性/操作教训时用 memory_add（进后台对账队列，"
            "本轮不生效）；用户给出持久偏好、或要修正 [画像] 里的既有内容时用 block_update；"
            "发现 [相关记忆] 里的事实已被证伪时用 memory_invalidate 作废它，不要留着互相矛盾的记忆。"
        )
        parts.append(
            "任务拆分：面对复杂任务（涉及多文件修改、多步骤验证、跨模块重构等），请先在首轮输出一个简短的"
            "子任务清单（用编号列表），然后逐个完成并验证。每完成一个子任务就明确标注"
            "'✓ 子任务 N 完成'，便于追踪进度。若某个子任务卡住，先跳过并继续后续子任务，"
            "最后统一汇报未完成项。这样即使迭代数耗尽，也能保证部分成果交付。"
        )
        parts.append(
            "子 Agent：你可以使用 spawn_subagent 工具把独立子任务（如代码审查、方案设计、文档生成、"
            "测试用例生成等）交给一个子 Agent 处理。子 Agent 是一次无工具的 LLM 推理调用，"
            "适合做可独立完成的推理类任务。传入清晰的 task 和 system_prompt（角色设定），"
            "可选 extra_context 提供代码片段等上下文。子 Agent 返回结果后，你负责整合并执行后续操作。"
            "合理使用子 Agent 可以避免主上下文被细节淹没，提高复杂任务的执行效率。"
        )
        parts.append(
            "输出格式：在工具调用迭代过程中保持输出简洁，避免把每一步的内心独白、英文碎碎念或执行日志写入正文。"
            "完成所有操作后，用结构化中文报告总结：先给总体结论，再用 ## 二级标题分节，每个修复点独立成段，"
            "代码用代码块包裹。标题、列表、代码块前后必须换行，确保 Markdown 渲染正确。"
        )

    return "\n\n".join(parts)
```

注意：函数顶部原有的 `from prompts import OPS_METHOD_CHANGE, OPS_METHOD_READONLY` 局部 import 删除（核心不再注入方法论）。`_current_time_str` 与 `_WORKFLOW_MODE_PROMPTS` 沿用模块现有 import/定义。

- [ ] **Step 5: 改调用点——身份段传参，删末尾追加**

把 `backend/routes/agent.py:983-994` 改为：

```python
    # M3 修复：提取 system prompt 构建为独立函数
    # 专家身份段在组装器内紧随基础身份注入（spec §5），不再末尾追加。
    final_system_prompt = _build_agent_system_prompt(
        req,
        profile_text,
        is_conversation_task,
        is_change=pre_intention.is_change if pre_intention else None,
        goal_text=goal_text,
        identity_section=build_identity_section(policy),
    )
```

（删除原 `_expert_identity = build_identity_section(policy)` 与 `if _expert_identity: final_system_prompt = ... + "\n\n" + _expert_identity` 三行。下方插件段 `PLUGIN_PROMPT_SECTION` 追加保持不变。）

- [ ] **Step 6: 跑守卫确认绿**

Run: `cd backend && python -m pytest tests/test_agent_system_prompt.py -q`
Expected: 全 PASS。

- [ ] **Step 7: 证伪核心中性守卫一次**

临时在 `_build_agent_system_prompt` 的 `parts.append(BASE_IDENTITY_PROMPT)` 后加一行 `parts.append(SSH_MODE_PROMPT)`（并在函数顶部临时 `from prompts import SSH_MODE_PROMPT`），跑 `test_core_prompt_is_domain_neutral` → 必须 FAIL；删除该行与临时 import 再跑 → PASS。

- [ ] **Step 8: 跑存量运维相关回归 + ruff + commit**

Run: `cd backend && python -m pytest tests/test_agent_system_prompt.py tests/test_expert_builtin_packs.py tests/test_expert_policy_legacy_noop.py -q && python -m ruff check --select F,E9 routes/agent.py prompts.py`
Expected: 全 PASS，ruff 0 命中。

```bash
git add backend/prompts.py backend/routes/agent.py backend/tests/test_agent_system_prompt.py
git commit -m "feat(prompt): 核心 prompt 中性化，专家身份段前置，任务性质提示通用化"
```

---

### Task 3: DB 迁移（四内置种子 + general_ops 升级 + schedule 专家列）

**Files:**
- Modify: `backend/database.py:104`（_SCHEMA_VERSION）、`:641-662`（schedules DDL）、`:791-807`（种子块）、增量区
- Modify: `backend/tests/test_expert_routes_crud.py:30-42`（种子测试）
- Test: `backend/tests/test_expert_migration.py`（新建）

**Interfaces:**
- Consumes: Task 1 的四个 `builtin_*` 工厂。
- Produces: 旧库 init 后四内置齐全；`general_ops` 内置行升级为运维包；`schedules.expert_slug` 列（存量回填 `general_ops`，新建默认 `general`）；用户自定义行（is_builtin=0）逐字节不动。

- [ ] **Step 1: 写迁移守卫（先红）**

Create `backend/tests/test_expert_migration.py`:

```python
"""DB 迁移守卫：四内置齐全 / general_ops 升级 / 用户行不动 / schedule 回填 / 二次幂等。

证伪：去掉 UPDATE 的 is_builtin=1 条件 → 用户行被改 → 红。
"""
import json

import database as db
from agent.expert_policy import builtin_general_ops


def _experts():
    return {r["slug"]: r for r in db.list_experts()}


def test_four_builtins_seeded(base_db):
    rows = _experts()
    for slug in ("general", "general_ops", "research", "copywriting"):
        assert slug in rows, slug
        assert rows[slug]["is_builtin"] == 1
        assert rows[slug]["enabled"] == 1


def test_general_ops_builtin_upgraded_to_ops_pack(base_db):
    row = _experts()["general_ops"]
    assert row["name"] == "运维专家"
    assert "智能运维助手" in row["identity_prompt"]
    assert "远程运维方法" in row["identity_prompt"]


def test_domain_packs_persist_explicit_empty_plugins(base_db):
    for slug in ("research", "copywriting"):
        row = _experts()[slug]
        assert json.loads(row["plugin_ids_json"]) == []
        assert row["memory_scope"] == "global"


def test_user_custom_row_untouched_by_migration(base_db):
    """用户复制行（is_builtin=0）逐字节不动。"""
    created = db.create_expert("general_ops", "我的运维", identity_prompt="自定义身份")
    # 手动标记为用户行（create_expert 默认 is_builtin=0）
    before = db.get_expert(created["id"])
    assert before["is_builtin"] == 0
    db.init_db(base_db)  # 二次 init 触发升级 UPDATE
    after = db.get_expert(created["id"])
    assert after["identity_prompt"] == "自定义身份"
    assert after["name"] == "我的运维"
    assert after["revision"] == before["revision"]


def test_second_init_is_idempotent(base_db):
    db.init_db(base_db)
    snapshot = {s: dict(r) for s, r in _experts().items()}
    db.init_db(base_db)
    again = {s: dict(r) for s, r in _experts().items()}
    for slug in snapshot:
        for key in ("name", "identity_prompt", "revision", "tool_names_json", "plugin_ids_json"):
            assert snapshot[slug][key] == again[slug][key], (slug, key)


def test_schedule_expert_slug_backfill_and_default(base_db):
    # 存量行（迁移前创建）回填 general_ops
    old = db.create_schedule({"name": "旧巡检", "task_prompt": "看下磁盘"})
    db.init_db(base_db)  # 触发回填（仅 _ddl_applied=False 时）
    # 注意：base_db 已 init 过一次（version=10），此处再 init 是 _ddl_applied=True，
    # 回填不重跑——存量回填的真实路径由 test_legacy_schedule_backfill 覆盖。
    assert db.get_schedule(old["id"])["expert_slug"] in ("general", "general_ops")
    new = db.create_schedule({"name": "新任务", "task_prompt": "写周报"})
    assert db.get_schedule(new["id"])["expert_slug"] == "general"


def test_legacy_schedule_backfill(tmp_workspace):
    """模拟旧库：先建无 expert_slug 列的 schedules 行，再升级 → 回填 general_ops。"""
    import os
    import sqlite3
    db_path = os.path.join(tmp_workspace, "legacy.db")
    db._close_all_connections()
    db.init_db(db_path)
    sid = db.create_schedule({"name": "存量", "task_prompt": "巡检"})["id"]
    # 人为降版 + 删列，模拟旧库结构
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    db._close_all_connections()
    db.init_db(db_path)  # version 9 → 10，_ddl_applied=False → 回填
    assert db.get_schedule(sid)["expert_slug"] == "general_ops"
    db._close_all_connections()
```

注意：`test_legacy_schedule_backfill` 的「删列」在 SQLite 需重建表，较繁琐；若实现时确认 `ALTER TABLE ADD COLUMN` 的增量补列在 version 9→10 路径上必然触发（列不存在才加），可简化为只降版不删列——回填的判据是 `_ddl_applied=False`（版本不命中），与列是否已存在无关。实现者按实际 init_db 结构调整该用例，但**必须保留「存量行回填 general_ops」这条断言**。

- [ ] **Step 2: 跑守卫确认红**

Run: `cd backend && python -m pytest tests/test_expert_migration.py -q`
Expected: FAIL（`general`/`research`/`copywriting` 未种；`expert_slug` 列不存在 → KeyError）。

- [ ] **Step 3: 升版 + schedules DDL 加列**

修改 `backend/database.py:104`：`_SCHEMA_VERSION = 9` → `_SCHEMA_VERSION = 10`。

在 schedules 建表 DDL（`:641-662`）的 `task_type` 行后加一行：
```sql
                -- 通用 harness 化：任务绑定的专家 slug；存量回填 general_ops，新建默认 general
                expert_slug     TEXT NOT NULL DEFAULT 'general',
```

- [ ] **Step 4: 增量区补列 + 一次性回填**

在 `backend/database.py` 增量区（`agent_jobs` out_offset 补列之后、`_migrate_db(conn)` 之前，约 `:727`）加：

```python
        # schedules 补 expert_slug（旧库）：列不存在才加，幂等。
        cur = conn.execute("PRAGMA table_info(schedules)")
        sched_cols = {row[1] for row in cur.fetchall()}
        if "expert_slug" not in sched_cols:
            conn.execute(
                "ALTER TABLE schedules ADD COLUMN expert_slug TEXT NOT NULL DEFAULT 'general'"
            )
        # 存量定时任务一次性回填 general_ops（保持旧运维行为）。只在版本不命中
        # （_ddl_applied=False，即本次是升级/建库）时跑：已是最新版本的库跳过，
        # 否则会把用户新建的 general 任务每次重启都重置成 general_ops。
        if not _ddl_applied:
            conn.execute(
                "UPDATE schedules SET expert_slug = 'general_ops' WHERE expert_slug = 'general'"
            )
```

- [ ] **Step 5: 四内置种子 + general_ops 升级**

把 `backend/database.py:791-807` 的现有 general_ops 种子块整段替换为：

```python
        # 四个内置专家种子（spec §6.1）。按 slug 判存在（而非 is_builtin）：用户复制出的
        # 自定义专家不该挡住种子落库。局部 import：避免模块加载期拉起 agent 包。
        from agent.expert_policy import (
            builtin_copywriting, builtin_general, builtin_general_ops, builtin_research,
        )
        for _factory in (builtin_general, builtin_general_ops, builtin_research, builtin_copywriting):
            _seed = _factory()
            conn.execute(
                "INSERT INTO experts "
                "(id, slug, name, description, identity_prompt, model_id, tool_names_json, "
                " plugin_ids_json, skill_ids_json, permission_ceiling, max_iterations, "
                " memory_scope, enabled, is_builtin, revision) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM experts WHERE slug = ?)",
                (
                    _new_id(), _seed["slug"], _seed["name"], _seed["description"],
                    _seed["identity_prompt"], _seed["model_id"], _seed["tool_names_json"],
                    _seed["plugin_ids_json"], _seed["skill_ids_json"], _seed["permission_ceiling"],
                    _seed["max_iterations"], _seed["memory_scope"], _seed["enabled"],
                    _seed["is_builtin"], _seed["revision"], _seed["slug"],
                ),
            )
        # 内置运维包升级：旧库 general_ops 行（空身份）→ 代码权威的运维包配置。
        # 只碰 is_builtin=1；用户复制行（is_builtin=0）逐字节不动。每次 init 都跑且幂等
        # （内置配置以代码为权威，API 不可编辑内置行）。
        _ops = builtin_general_ops()
        conn.execute(
            "UPDATE experts SET name = ?, description = ?, identity_prompt = ?, "
            " updated_at = datetime('now') WHERE slug = ? AND is_builtin = 1",
            (_ops["name"], _ops["description"], _ops["identity_prompt"], _ops["slug"]),
        )
```

- [ ] **Step 6: 更新 CRUD 种子测试**

修改 `backend/tests/test_expert_routes_crud.py`：`_builtin(client)`（`:30-32`）现在按 `DEFAULT_EXPERT_SLUG`（="general"）取行。把 `test_builtin_general_ops_is_seeded_on_init`（`:35-42`）改为显式取 general_ops 并新增四内置断言：

```python
def test_builtin_general_ops_is_seeded_on_init(client):
    rows = client.get("/api/experts").json()
    row = next(r for r in rows if r["slug"] == "general_ops")
    assert row["is_builtin"] == 1
    assert row["enabled"] == 1
    assert row["tool_names_json"] is None       # 继承，不是空集
    assert row["plugin_ids_json"] is None       # 内置继承：允许加载已启用插件
    assert row["permission_ceiling"] == "inherit"
    assert row["memory_scope"] == "inherit"
    assert "智能运维助手" in row["identity_prompt"]  # 升级为运维包


def test_all_four_builtins_seeded(client):
    rows = client.get("/api/experts").json()
    slugs = {r["slug"] for r in rows if r["is_builtin"] == 1}
    assert {"general", "general_ops", "research", "copywriting"} <= slugs
```

- [ ] **Step 7: 跑守卫确认绿 + 证伪一次**

Run: `cd backend && python -m pytest tests/test_expert_migration.py tests/test_expert_routes_crud.py -q`
Expected: 全 PASS。

证伪：临时把 Step 5 的 UPDATE 里 `AND is_builtin = 1` 删掉，跑 `test_user_custom_row_untouched_by_migration` → 必须 FAIL；恢复 → PASS。

- [ ] **Step 8: ruff + commit**

Run: `cd backend && python -m ruff check --select F,E9 database.py tests/test_expert_migration.py`
Expected: 0 命中。

```bash
git add backend/database.py backend/tests/test_expert_migration.py backend/tests/test_expert_routes_crud.py
git commit -m "feat(db): schema v10 种四内置专家，升级运维包，schedule 绑专家 slug"
```

---

### Task 4: 头less 路径专家接线

**Files:**
- Modify: `backend/agent/headless_run.py:181-191`（签名）、装配段（system_prompt 拼接）
- Modify: `backend/agent/schedule_engine.py:605`（run 方法解析专家）、`:822-853`（构建器）
- Modify: `backend/runbook_engine.py:293-300`、`:432-439`（传 expert_slug）
- Modify: `backend/routes/schedules.py:32-68`（ScheduleIn/SchedulePatch 加 expert_slug）
- Modify: `backend/database.py`（create_schedule/update_schedule/_SCHEDULE_COLS 收 expert_slug）
- Test: `backend/tests/test_headless_expert_wiring.py`（新建）

**Interfaces:**
- Consumes: Task 1 工厂、Task 3 的 `schedules.expert_slug` 列、`resolve_expert`/`build_identity_section`/`db.get_expert_by_slug`。
- Produces:
  - `run_headless_once(..., expert_slug: str = "general") -> HeadlessRunResult`
  - `_build_schedule_system_prompt(sched: dict, identity_section: str = "") -> str`
  - `ScheduleIn.expert_slug: str = "general"`、`SchedulePatch.expert_slug: Optional[str] = None`

- [ ] **Step 1: 写接线守卫（先红）**

Create `backend/tests/test_headless_expert_wiring.py`:

```python
"""头less 专家接线守卫：schedule 去硬编码 / runbook 解析运维身份 / slug fail-closed。

证伪：把 SSH_MODE_PROMPT 硬编码注回 schedule 构建器 → 红。
"""
import pytest

from agent.schedule_engine import _build_schedule_system_prompt
from prompts import BASE_IDENTITY_PROMPT, SSH_MODE_PROMPT


def test_schedule_prompt_no_hardcoded_ops_identity():
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": "h"})
    assert SSH_MODE_PROMPT not in prompt
    assert "智能运维助手" not in prompt
    assert BASE_IDENTITY_PROMPT in prompt


def test_schedule_prompt_keeps_no_host_guard_and_report_format():
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": ""})
    assert "未指定目标主机" in prompt
    assert "定时任务" in prompt


def test_schedule_prompt_inserts_identity_section():
    section = "专家角色设定（运维专家）：\n运维身份测试段"
    prompt = _build_schedule_system_prompt({"name": "s", "target_host": "h"}, section)
    assert section in prompt
    assert prompt.index(BASE_IDENTITY_PROMPT) < prompt.index(section)


def test_schedule_prompt_empty_identity_adds_no_bytes():
    a = _build_schedule_system_prompt({"name": "s", "target_host": "h"}, "")
    b = _build_schedule_system_prompt({"name": "s", "target_host": "h"})
    assert a == b


@pytest.mark.asyncio
async def test_runbook_resolves_ops_identity(base_db, monkeypatch):
    """runbook 无头 run 传 general_ops → system_prompt 含运维身份段。"""
    import agent.headless_run as hr

    captured = {}

    class _FakeHarness:
        def __init__(self, *a, **k):
            pass

        def assemble(self, *a, **k):
            pass

        async def run(self, **kw):
            captured["system_prompt"] = kw.get("system_prompt", "")
            yield type("Ev", (), {"kind": "done", "payload": {"content": "ok", "state": "completed"}})()

    monkeypatch.setattr(hr, "Harness", _FakeHarness)
    monkeypatch.setattr(hr, "resolve_model", lambda: {
        "base_url": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 8000,
    })
    monkeypatch.setattr(hr, "resolve_run_environment", _fake_resolution)
    await hr.run_headless_once(
        "步骤任务", run_id="r1", conversation_id="runbook-x",
        ssh_session_id="", expert_slug="general_ops",
    )
    assert "智能运维助手" in captured["system_prompt"]
    assert "远程运维方法" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_headless_unknown_slug_fail_closed(base_db, monkeypatch):
    import agent.headless_run as hr
    monkeypatch.setattr(hr, "resolve_model", lambda: {
        "base_url": "http://x", "api_key": "k", "model_id": "m", "max_tokens": 8000,
    })
    with pytest.raises(ValueError):
        await hr.run_headless_once(
            "t", run_id="r2", conversation_id="c", expert_slug="no-such-expert",
        )


async def _fake_resolution(*a, **k):
    from agent.run_environment import RunEnvironmentResolution
    return RunEnvironmentResolution(source="none", effective="test", host="")
```

注意：`_fake_resolution` 的返回类型以 `agent/run_environment.py` 实际 `RunEnvironmentResolution` 构造签名为准（实现者核对字段名）；`_FakeHarness.run` 用 `type(...)` 造简易事件对象，避免依赖真实 contracts。若 `run_headless_once` 还调用 `StreamLLMClient`/`create_registry`/`CostTracker`，monkeypatch 一并替换为无副作用桩。实现者按实际装配链补齐桩，但**保留两条核心断言**（运维身份注入 / 未知 slug 抛错）。

- [ ] **Step 2: 跑守卫确认红**

Run: `cd backend && python -m pytest tests/test_headless_expert_wiring.py -q`
Expected: FAIL（schedule 仍硬编码 SSH_MODE_PROMPT；run_headless_once 无 expert_slug 参 → TypeError）。

- [ ] **Step 3: headless_run 加 expert_slug + 解析拼接**

修改 `backend/agent/headless_run.py`：签名（`:181-191`）加关键字参 `expert_slug: str = "general"`（放在 `system_prompt` 之后）。在 `model_cfg` 解析之后、`harness = Harness(...)` 之前加专家解析：

```python
    # 专家接线（spec §6.2）：与交互路径同一套 resolve_expert / build_identity_section。
    # slug 查不到 fail-closed 抛错，不静默降级成「无身份」。
    import database as db
    from agent.expert_policy import build_identity_section, resolve_expert
    expert_row = await asyncio.to_thread(db.get_expert_by_slug, expert_slug)
    if expert_row is None:
        raise ValueError(f"专家不存在: {expert_slug}")
    expert_policy = resolve_expert(expert_row)
    _identity = build_identity_section(expert_policy)
    if _identity:
        system_prompt = (system_prompt + "\n\n" + _identity) if system_prompt else _identity
```

（`system_prompt` 是函数参数，后续 `harness.run(system_prompt=system_prompt, ...)` 自动带上拼接结果，无需改 run 调用。）

- [ ] **Step 4: schedule 构建器去硬编码 + 接收身份段**

修改 `backend/agent/schedule_engine.py:822-853` 的 `_build_schedule_system_prompt`：

```python
def _build_schedule_system_prompt(sched: dict, identity_section: str = "") -> str:
    parts = []
    # 无目标主机守卫段先落（只读 sched，不被可选上下文失败一起吞掉）
    if not (sched.get("target_host") or "").strip():
        parts.append(
            "[执行环境] 本次任务未指定目标主机，因此没有可用的 SSH 会话。"
            "不要调用任何远程工具（shell_exec / job_run / service_manage / "
            "log_read 等一律以「缺少 SSH 会话」失败）；请只基于本地可得的信息"
            "完成任务，并在报告开头写明本次没有可连接的目标主机。"
        )
    try:
        from prompts import BASE_IDENTITY_PROMPT, _current_time_str
        parts.append(f"[系统时间] 当前时间：{_current_time_str()}。")
        parts.append(BASE_IDENTITY_PROMPT)
    except Exception as e:
        logger.warning("定时任务系统提示词的上下文段构建失败，已降级 (schedule=%s): %s",
                       sched.get("sid") or sched.get("name") or "?", e)
    # 专家身份段紧随中性基础身份（与交互路径同序）
    if identity_section:
        parts.append(identity_section)
    parts.append(
        "你现在以「定时任务」方式自动执行。请独立完成给定任务，"
        "并输出结构化中文报告：先给总体结论，再用 ## 二级标题分节说明发现的问题、"
        "已执行的操作与后续建议。"
    )
    if _is_inspection_task(sched):
        parts.append(_INSPECT_VERDICT_HINT)
    parts.append("请以只读、安全的操作为主，涉及破坏性操作时先说明计划再执行。")
    return "\n\n".join(parts)
```

修改 run 方法（`:605`）`system_prompt = _build_schedule_system_prompt(sched)` 为：

```python
        # 专家接线：存量任务迁移时已绑 general_ops，新建默认 general（spec §6.2）
        import database as db
        from agent.expert_policy import build_identity_section, resolve_expert
        _expert_slug = (sched.get("expert_slug") or "general").strip() or "general"
        _expert_row = await asyncio.to_thread(db.get_expert_by_slug, _expert_slug)
        if _expert_row is None:
            raise ValueError(f"定时任务绑定的专家不存在: {_expert_slug}")
        _identity_section = build_identity_section(resolve_expert(_expert_row))
        system_prompt = _build_schedule_system_prompt(sched, _identity_section)
```

- [ ] **Step 5: runbook 两处传 general_ops**

修改 `backend/runbook_engine.py:293-300`（步骤执行）与 `:432-439`（失败诊断）的 `run_headless_once(...)` 调用，各加一行 `expert_slug="general_ops",`（剧本天然运维）。

- [ ] **Step 6: schedule 路由 + DB CRUD 收 expert_slug**

修改 `backend/routes/schedules.py`：
- `ScheduleIn`（`:32-48`）加 `expert_slug: str = "general"`。
- `SchedulePatch`（`:51-68`）加 `expert_slug: Optional[str] = None`。

修改 `backend/database.py`：
- `_SCHEDULE_COLS`（`:4926-4930`）加 `expert_slug`（放在 `task_type` 后）。
- `create_schedule`（`:4967-5003`）INSERT 列与 VALUES 加 `expert_slug`，取值 `data.get("expert_slug") or "general"`。
- `update_schedule`（`:5006-5011`）`allowed` 集加 `"expert_slug"`。

- [ ] **Step 7: 跑守卫确认绿 + 证伪一次**

Run: `cd backend && python -m pytest tests/test_headless_expert_wiring.py tests/test_expert_migration.py -q`
Expected: 全 PASS。

证伪：临时在 `_build_schedule_system_prompt` 的 `parts.append(BASE_IDENTITY_PROMPT)` 后加 `parts.append(SSH_MODE_PROMPT)`（顶部临时 import），跑 `test_schedule_prompt_no_hardcoded_ops_identity` → FAIL；恢复 → PASS。

- [ ] **Step 8: 跑 schedule/runbook 存量回归 + ruff + commit**

Run: `cd backend && python -m pytest tests/ -q -k "schedule or runbook or headless" && python -m ruff check --select F,E9 agent/headless_run.py agent/schedule_engine.py runbook_engine.py routes/schedules.py database.py`
Expected: 全 PASS，ruff 0 命中。

```bash
git add backend/agent/headless_run.py backend/agent/schedule_engine.py backend/runbook_engine.py backend/routes/schedules.py backend/database.py backend/tests/test_headless_expert_wiring.py
git commit -m "feat(headless): runbook/schedule 接专家解析，schedule 去运维身份硬编码"
```

---

### Task 5: 运维专家建议（后端谓词 + SSE 事件）

**Files:**
- Create: `backend/agent/expert_suggest.py`
- Modify: `backend/routes/agent.py`（初始 state 事件后加建议 yield）
- Modify: `backend/agent/harness/telemetry.py:104-133`（_KIND_MAP）
- Modify: `backend/tests/test_trace_projection_keys.py:190-200`（ROUTE_LEVEL_KINDS 豁免）
- Test: `backend/tests/test_expert_suggestion.py`（新建）

**Interfaces:**
- Consumes: `intention._COMPILED_CHANGE` / `_any_match_wordbounded` / `_precompile_markers`；`builtin_general_ops`。
- Produces:
  - `expert_suggest.should_suggest_ops(task: str, *, expert_selected: bool, is_conversation: bool) -> bool`
  - SSE 事件 `{"type": "expert_suggestion", "slug", "name", "reason", "task", "run_id"}`

- [ ] **Step 1: 写谓词守卫（先红）**

Create `backend/tests/test_expert_suggestion.py`:

```python
"""运维专家建议谓词守卫：触发/不触发边界 + 只读运维覆盖。

关键词级规则（复用变更动词表 + 运维名词小词表），不是新分类器。
证伪：让失败路径抛异常 → 调用方静默 no-op 守卫红（见路由侧）。
"""
import pytest

from agent.expert_suggest import should_suggest_ops


@pytest.mark.parametrize("task", [
    "重启 nginx",          # 变更动词
    "看下磁盘空间",        # 只读运维（名词命中，变更动词不命中）
    "查一下端口占用",
    "docker 容器挂了",
    "清理日志",
])
def test_ops_signal_triggers(task):
    assert should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_selected_expert_suppresses():
    assert not should_suggest_ops("重启 nginx", expert_selected=True, is_conversation=False)


def test_conversation_suppresses():
    assert not should_suggest_ops("你好", expert_selected=False, is_conversation=True)


@pytest.mark.parametrize("task", ["写一首诗", "帮我写周报", "翻译这段英文", ""])
def test_non_ops_task_does_not_trigger(task):
    assert not should_suggest_ops(task, expert_selected=False, is_conversation=False)


def test_none_task_safe():
    assert not should_suggest_ops(None, expert_selected=False, is_conversation=False)
```

- [ ] **Step 2: 跑守卫确认红**

Run: `cd backend && python -m pytest tests/test_expert_suggestion.py -q`
Expected: FAIL（`ModuleNotFoundError: agent.expert_suggest`）。

- [ ] **Step 3: 实现谓词模块**

Create `backend/agent/expert_suggest.py`:

```python
"""运维专家建议谓词 —— 关键词级规则，不是新分类器（spec §6.3）。

触发：未选专家 + 非纯对话 +（变更动词 ∨ 运维名词）。复用 intention 的预编译变更动词表
与词边界匹配器；运维名词小词表覆盖只读运维（"看下磁盘"不命中变更动词）。
误报成本低（非阻塞 chip），漏报由用户手选兜底。判定失败由调用方静默 no-op。
"""
from agent.intention import (
    _COMPILED_CHANGE,
    _any_match_wordbounded,
    _precompile_markers,
)

_OPS_NOUNS = [
    "服务器", "磁盘", "端口", "进程", "服务", "日志", "容器", "主机",
    "内存", "负载", "数据库",
    "systemctl", "docker", "nginx", "kubernetes", "k8s", "mysql", "redis",
]
_COMPILED_OPS_NOUNS = _precompile_markers(_OPS_NOUNS)


def should_suggest_ops(task: str, *, expert_selected: bool, is_conversation: bool) -> bool:
    if expert_selected or is_conversation:
        return False
    text = task or ""
    if not text.strip():
        return False
    lowered = text.lower()
    if _any_match_wordbounded(_COMPILED_CHANGE, lowered, text):
        return True
    return _any_match_wordbounded(_COMPILED_OPS_NOUNS, lowered, text)
```

- [ ] **Step 4: 跑谓词守卫确认绿**

Run: `cd backend && python -m pytest tests/test_expert_suggestion.py -q`
Expected: 全 PASS。

- [ ] **Step 5: 登记 SSE 事件 + 路由发射**

修改 `backend/agent/harness/telemetry.py` 的 `_KIND_MAP`（`:104-133`），在 `"plan_mode_block"` 行后加：
```python
        # 路由层直发（routes/agent.py，不经 harness TraceEvent）：运维专家建议 chip
        "expert_suggestion": "expert_suggestion",
```

修改 `backend/routes/agent.py`：在初始 state 事件（`:1290` `yield sse_event({"type": "state", ...})`）之后加：
```python
        # 运维专家建议（非阻塞增益，spec §6.3）：未选专家 + 非纯对话 + 运维信号 → chip。
        # 判定失败静默 no-op，绝不拖 run。当前 run 继续以通用身份跑（能力不缺，只缺方法论纪律）。
        try:
            from agent.expert_policy import builtin_general_ops
            from agent.expert_suggest import should_suggest_ops
            if should_suggest_ops(
                req.task or "",
                expert_selected=bool(req.expert_id),
                is_conversation=is_conversation_task,
            ):
                _ops = builtin_general_ops()
                yield sse_event({
                    "type": "expert_suggestion",
                    "slug": _ops["slug"],
                    "name": _ops["name"],
                    "reason": "检测到运维任务，建议切到运维专家",
                    "task": req.task or "",
                    "run_id": run_id,
                })
        except Exception as e:
            logger.warning("运维专家建议判定失败（跳过）: %s", e)
```

- [ ] **Step 6: 更新跨层守卫（ROUTE_LEVEL_KINDS 豁免）**

修改 `backend/tests/test_trace_projection_keys.py:190-200` 的 `test_kind_map_covers_every_emitted_kind`：

```python
# 路由层直发的事件（不经 harness TraceEvent，与 routes/agent.py 初始 state 事件同模式）：
# 不是死条目，只是发射点在路由而非 harness，故从「死条目」检查里豁免。
ROUTE_LEVEL_KINDS = {"expert_suggestion"}


def test_kind_map_covers_every_emitted_kind():
    emitted = _emitted_kinds()
    assert {"clarification_needed", "plan_mode_block", "budget_extended"} <= emitted, (
        "扫描没走进 reins/ —— 这条守卫会因为读不到东西而恒真")
    declared = set(SSEAdapter._KIND_MAP)
    missing = emitted - declared
    assert not missing, (
        f"这些 kind 上了 Wire 却不在 _KIND_MAP 里（只靠恒等兜底，清单不再是全集）: "
        f"{sorted(missing)}")
    assert ROUTE_LEVEL_KINDS <= declared, (
        f"路由层事件必须在 _KIND_MAP 登记: {sorted(ROUTE_LEVEL_KINDS - declared)}")
    unused = declared - emitted - ROUTE_LEVEL_KINDS
    assert not unused, f"_KIND_MAP 里有 harness 从不发出的死条目: {sorted(unused)}"
```

（`test_frontend_union_matches_backend_registry` 比对 `_KIND_MAP.values()` == 前端 type 联合——Task 6 给前端联合加 `expert_suggestion` 后两侧一致。本任务先改后端，该守卫会暂时红，Task 6 补齐前端后绿；这是跨任务耦合，执行时 Task 5/6 须连续完成再跑该守卫。）

- [ ] **Step 7: 跑后端守卫 + ruff**

Run: `cd backend && python -m pytest tests/test_expert_suggestion.py tests/test_trace_projection_keys.py::test_kind_map_covers_every_emitted_kind -q && python -m ruff check --select F,E9 agent/expert_suggest.py routes/agent.py agent/harness/telemetry.py`
Expected: PASS（`test_frontend_union_matches_backend_registry` 待 Task 6），ruff 0 命中。

- [ ] **Step 8: commit（与 Task 6 合并跑跨层守卫后再确认绿）**

```bash
git add backend/agent/expert_suggest.py backend/routes/agent.py backend/agent/harness/telemetry.py backend/tests/test_expert_suggestion.py backend/tests/test_trace_projection_keys.py
git commit -m "feat(suggest): 运维专家建议谓词与非阻塞 SSE 事件"
```

---

### Task 6: 前端建议 chip（切换并重跑 / 忽略）

**Files:**
- Modify: `src/api/sse.ts:17-93`（type 联合 + slug/name 字段）
- Modify: `src/stores/agent.ts`（expertSuggestion 态 + case + _reset 清理 + dismiss）
- Modify: `src/components/home/HomeComposer.vue:670`（chip 模板）、script（accept/ignore）
- Test: `src/stores/agent.test.ts`（新增 case 守卫）
- Test: `src/components/home/HomeComposer.expert-suggestion.guards.test.ts`（新建）

**Interfaces:**
- Consumes: Task 5 的 `expert_suggestion` 事件（slug/name/reason/task）。
- Produces:
  - `AgentSSEEvent.type` 含 `'expert_suggestion'`；`slug?: string`、`name?: string`
  - agent store：`expertSuggestion: Ref<{slug,name,reason,task}|null>`、`dismissExpertSuggestion()`
  - HomeComposer：建议 chip + `acceptExpertSuggestion()` / `ignoreExpertSuggestion()`

- [ ] **Step 1: 写 store 守卫（先红）**

在 `src/stores/agent.test.ts` 末尾追加：

```typescript
describe('expert_suggestion 事件', () => {
  beforeEach(() => {
    runs.length = 0
    setActivePinia(createPinia())
  })

  it('回填建议态（slug/name/reason/task）', () => {
    const { store, push } = startRun()
    push({ type: 'expert_suggestion', slug: 'general_ops', name: '运维专家', reason: '检测到运维任务', task: '重启 nginx' })
    expect(store.expertSuggestion).toEqual({
      slug: 'general_ops', name: '运维专家', reason: '检测到运维任务', task: '重启 nginx',
    })
  })

  it('dismiss 清空建议态', () => {
    const { store, push } = startRun()
    push({ type: 'expert_suggestion', slug: 'general_ops', name: '运维专家', task: 't' })
    store.dismissExpertSuggestion()
    expect(store.expertSuggestion).toBeNull()
  })

  it('新 run 清空上一轮建议（_reset）', () => {
    const h = startRun()
    h.push({ type: 'expert_suggestion', slug: 'general_ops', name: '运维专家', task: 't' })
    expect(h.store.expertSuggestion).not.toBeNull()
    h.store.runAgent({ task: 't2', conversationId: 'c2', modelId: 'm1' })
    expect(h.store.expertSuggestion).toBeNull()
  })

  it('缺 slug 不回填（防御脏事件）', () => {
    const { store, push } = startRun()
    push({ type: 'expert_suggestion', name: '运维专家' })
    expect(store.expertSuggestion).toBeNull()
  })
})
```

- [ ] **Step 2: 跑守卫确认红**

Run: `npx vitest run src/stores/agent.test.ts -t "expert_suggestion"`
Expected: FAIL（`store.expertSuggestion` undefined）。

- [ ] **Step 3: 扩 SSE 类型**

修改 `src/api/sse.ts:20` 的 type 联合，在 `'done'` 前加 `'expert_suggestion' |`：
```typescript
  type: 'state' | 'content' | 'reasoning' | 'tool_call_start' | 'tool_call_result' | 'warning' | 'error' | 'human_help_needed' | 'permission_needed' | 'permission_expired' | 'permission_explain' | 'intention' | 'tool_deliver' | 'budget_extended' | 'reflection_request' | 'plan_mode_block' | 'clarification_needed' | 'clarification_done' | 'expert_suggestion' | 'done'
```
在接口字段区（`expired?: boolean` 后）加：
```typescript
  /** expert_suggestion 专用：建议切换的专家 slug 与展示名 */
  slug?: string
  name?: string
```
（`reason?` / `task?` 字段已存在，复用。）

- [ ] **Step 4: agent store 加态 + case + 清理**

修改 `src/stores/agent.ts`：
- 在状态声明区（`intentionInfo` 附近）加：
```typescript
  /** 运维专家建议（非阻塞 chip）：后端 expert_suggestion 事件回填，用户切换/忽略后清空 */
  const expertSuggestion = ref<{ slug: string; name: string; reason: string; task: string } | null>(null)
```
- `_handleEvent` 的 switch（`:604`）在 `case 'done':` 前加：
```typescript
      case 'expert_suggestion':
        if (event.slug) {
          expertSuggestion.value = {
            slug: event.slug,
            name: event.name || event.slug,
            reason: event.reason || '',
            task: event.task || '',
          }
        }
        break
```
- `_reset()`（`:439-467`）加 `expertSuggestion.value = null`。
- 加函数：
```typescript
  function dismissExpertSuggestion() {
    expertSuggestion.value = null
  }
```
- return 对象导出 `expertSuggestion`、`dismissExpertSuggestion`。

- [ ] **Step 5: 跑 store 守卫确认绿 + 跨层守卫**

Run: `npx vitest run src/stores/agent.test.ts -t "expert_suggestion"`
Expected: PASS。
Run: `cd backend && python -m pytest tests/test_trace_projection_keys.py -q`
Expected: 全 PASS（前端联合已补 expert_suggestion，两侧一致）。

- [ ] **Step 6: 写 HomeComposer chip 守卫（先红）**

Create `src/components/home/HomeComposer.expert-suggestion.guards.test.ts`。参照现有 `HomeWorkspace.layout.test.ts` 的组件挂载模式（mock 各 store）。核心断言：

```typescript
/**
 * 运维专家建议 chip 守卫：渲染 [切换并重跑][忽略]，非阻塞。
 * 证伪：删掉 chip 模板 → 红。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

// mock 依赖 store（按 HomeComposer 实际 import 面补齐）
vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({
    isRunning: false,
    expertSuggestion: { slug: 'general_ops', name: '运维专家', reason: '检测到运维任务', task: '重启 nginx' },
    dismissExpertSuggestion: vi.fn(),
    abort: vi.fn(),
  }),
}))
// ... 其余 store mock 参照 HomeWorkspace.layout.test.ts 的最小面

describe('HomeComposer 专家建议 chip', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('有建议时渲染切换并重跑 / 忽略两个动作', async () => {
    const { default: HomeComposer } = await import('@/components/home/HomeComposer.vue')
    const wrapper = mount(HomeComposer, { global: { stubs: { teleport: true } } })
    const text = wrapper.text()
    expect(text).toContain('运维专家')
    expect(text).toContain('切换并重跑')
    expect(text).toContain('忽略')
  })
})
```

注意：HomeComposer 依赖较多 store/composable，实现者参照同目录现有测试（`HomeWorkspace.layout.test.ts`）补齐 mock 面使组件可挂载；**保留「渲染两个动作」断言**。若全量挂载成本过高，可退化为对 chip 子模板的源码守卫（断言模板含 `切换并重跑`/`忽略`/`acceptExpertSuggestion`），但优先组件级。

- [ ] **Step 7: HomeComposer 加 chip 模板 + 处理函数**

修改 `src/components/home/HomeComposer.vue`：
- 模板：在 `<div class="hcc">`（`:670`）之后、`<textarea>`（`:671`）之前插入：
```html
    <!-- 运维专家建议（非阻塞）：当前 run 继续跑，用户可切换并重跑或忽略 -->
    <div v-if="agent.expertSuggestion" class="hcc-suggest" role="status">
      <span class="hcc-suggest-text">{{ agent.expertSuggestion.reason }}：{{ agent.expertSuggestion.name }}</span>
      <button type="button" class="hcc-suggest-accept" @click="acceptExpertSuggestion">切换并重跑</button>
      <button type="button" class="hcc-suggest-ignore" @click="ignoreExpertSuggestion">忽略</button>
    </div>
```
- script：加处理函数（`agent`/`sshAi`/`expertsStore`/`toast` 均为现有实例）：
```typescript
async function acceptExpertSuggestion() {
  const sug = agent.expertSuggestion
  if (!sug) return
  const expert = expertsStore.experts.find(e => e.slug === sug.slug)
  const task = sug.task
  agent.dismissExpertSuggestion()
  if (!expert) {
    toast.error(`未找到专家 ${sug.name}`)
    return
  }
  sshAi.setSelectedExpert(expert.id)
  agent.abort()
  if (task.trim()) {
    await sshAi.sendMessage(task)
  }
}
function ignoreExpertSuggestion() {
  agent.dismissExpertSuggestion()
}
```
- 样式：在组件 `<style>`（或同目录 CSS，遵循项目 8-SFC 外提约定）加 `.hcc-suggest` 一族（柔和提示条，焦点态用淡 glow 不用硬 outline，遵循项目焦点规范）。

- [ ] **Step 8: 跑 chip 守卫 + typecheck + vitest 全量**

Run: `npx vitest run src/components/home/HomeComposer.expert-suggestion.guards.test.ts src/stores/agent.test.ts`
Expected: PASS。
Run: `npm run --silent typecheck`
Expected: 0 错误。

- [ ] **Step 9: commit**

```bash
git add src/api/sse.ts src/stores/agent.ts src/components/home/HomeComposer.vue src/stores/agent.test.ts src/components/home/HomeComposer.expert-suggestion.guards.test.ts
git commit -m "feat(ui): 运维专家建议 chip，切换并重跑/忽略，非阻塞"
```

---

### Task 7: 全量门禁 + 文档双语同步

**Files:**
- Modify: `README.md`、`README.en.md`（通用身份 + 四内置专家）
- Modify: `CONTEXT.md`（术语：专家/通用身份）
- 无新代码。

**Interfaces:**
- Consumes: Task 1-6 全部成果。
- Produces: ci-check.sh 全量绿；双语 README 与术语表同步。

- [ ] **Step 1: 更新 README.md**

在 README.md「Harness 与执行循环」节（`:42-47`）补一条通用身份说明，并新增「专家（运行策略配置包）」小节（与现有「后处理与技能」节同级）：

```markdown
**专家与通用身份**

- 默认身份是通用 AI 助手；运维、研究、文案是可选专家包，领域不硬编码、可增删。
- 四个内置专家：通用（general，全继承默认）、运维专家（general_ops，运维角色 + 变更/只读方法论 + 运维工具纪律）、研究（research）、文案（copywriting）。研究与文案收紧到无执行工具、无插件、记忆限 global。
- 专家只能收紧不能放宽：工具/技能求交、权限取更严格者、迭代取 min、记忆 scope 只收缩；配置损坏 fail-closed。
- 未选专家时检测到运维信号（变更动词或运维名词）会在输入区上方弹非阻塞建议 chip，可「切换并重跑」或「忽略」；不静默切换。
- 无人值守：剧本（runbook）固定走运维专家；定时任务可绑专家，存量任务迁移时绑运维专家、新建默认通用。
```

把「运行前只读探测目标机…按变更 / 只读任务注入对应的操作纪律」（`:46`）改为「运行前只读探测目标机发行版、init、包管理器与登录身份，写入主机画像；任务性质（只读 / 可能变更）作为通用元数据前置注入，运维方法论随运维专家身份段全文注入。」

- [ ] **Step 2: 同步 README.en.md**

在 README.en.md 对应节逐条翻译 Step 1 的新增内容（章节、表格、代码块逐节对应，遵循双语铁律）。英文术语：Expert (run-policy pack) / general / general_ops (Ops Expert) / research / copywriting / fail-closed / non-blocking suggestion chip。

- [ ] **Step 3: 更新 CONTEXT.md 术语表**

在 CONTEXT.md 术语表加一行：

```markdown
| **专家**（Expert，`experts` 表） | 运行策略配置包：身份 / 模型 / 工具 / 技能 / 安全姿态 / 迭代 / 记忆，只能收紧不能放宽。默认身份是通用 harness（`general`），运维是 `general_ops` 专家包（角色 + 方法论两变体 + 工具纪律全文进 identity_prompt） | 与技能（过程性知识）、插件（可执行能力）三层分工；方法论走身份段全文注入而非技能——SkillRein 只注入目录+片段，做成技能会让纪律退化为可选加载 |
```

- [ ] **Step 4: 跑全量门禁（约 20 分钟，期间不改树）**

Run: `bash scripts/ci-check.sh`
Expected: 全部检查通过 ✅（后端 pytest / ruff F,E9 / vue-tsc / vitest / electron-vite build / hillgw selftest）。

**门禁运行期间不得改动工作树。** 若任一项失败，定位修复后重跑该项靶向命令，再重跑全量。

- [ ] **Step 5: 验收对照（spec §10）**

逐条核对：
1. 未选专家中性身份（`test_core_prompt_is_domain_neutral` 绿）✓
2. 选运维专家语义等价（`test_ops_pack_identity_contains_role_and_both_methods_and_discipline` + 存量运维测试绿）✓
3. 研究/文案执行工具与插件物理缺席（`test_domain_pack_*` 绿）✓
4. 旧库迁移四内置齐全、用户行不动、二次幂等（`test_expert_migration.py` 绿）✓
5. 无人值守经专家包保持方法论、schedule 无硬编码（`test_headless_expert_wiring.py` 绿）✓
6. 建议 chip 触发/非阻塞/切换并重跑（`test_expert_suggestion.py` + 前端守卫绿）✓
7. ci-check.sh 全量绿 ✓

- [ ] **Step 6: commit**

```bash
git add README.md README.en.md CONTEXT.md
git commit -m "docs: 通用 harness 身份与四内置专家，双语同步 + 术语表"
```

---

## Self-Review（作者自查，已执行）

**1. Spec 覆盖：**
- §4.1 核心中性 + 意图降级 → Task 2（BASE_IDENTITY_PROMPT、任务性质提示、conversation 门控不动）。
- §4.2 三块运维文本进 identity_prompt → Task 1（OPS_IDENTITY_PROMPT）+ Task 2（核心删运维段）。
- §4.3 四内置专家表（plugin_ids=() / memory global）→ Task 1 工厂 + Task 3 种子。
- §5 组装新序 + 身份段前置 + 任务性质前置 → Task 2。
- §6.1 DB 迁移 → Task 3。§6.2 头less 接线 → Task 4。§6.3 建议交互 → Task 5 + 6。
- §7 错误处理（fail-closed / 幂等 / 建议 no-op）→ Task 3/4/5。
- §8 九条新守卫 + 存量迁移 → Task 1（packs/legacy/safety）、Task 2（system_prompt）、Task 3（migration/crud）、Task 4（headless）、Task 5（suggestion/trace）、Task 6（前端 chip）。对话门控既有守卫不动。
- §10 验收七条 → Task 7 Step 5 逐条对照。

**2. 占位符扫描：** 无 TBD/TODO；每个代码步含实际代码。两处「实现者按实际结构调整」（Task 3 旧库模拟、Task 4/6 测试桩）保留了核心断言与明确边界，非占位符——它们是对真实测试夹具依赖的诚实标注。

**3. 类型一致性：**
- `builtin_*() -> dict` 键集与现有 `builtin_general_ops` 一致（Task 1），Task 3 种子按这些键取值。
- `_build_agent_system_prompt(..., identity_section="")`（Task 2）与调用点 `identity_section=build_identity_section(policy)` 一致。
- `run_headless_once(..., expert_slug="general")`（Task 4）与 runbook 传 `expert_slug="general_ops"` 一致。
- `_build_schedule_system_prompt(sched, identity_section="")`（Task 4）与 run 方法传参一致。
- `should_suggest_ops(task, *, expert_selected, is_conversation)`（Task 5）与路由调用一致。
- SSE `expert_suggestion` 的 slug/name/reason/task 字段：后端发射（Task 5）↔ 前端类型（Task 6）↔ store 回填（Task 6）三处同名。
- `DEFAULT_EXPERT_SLUG="general"`（Task 1）贯穿 Task 3 种子 / Task 4 默认 / CRUD 测试。