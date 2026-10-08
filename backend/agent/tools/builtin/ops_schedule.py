"""
定时任务管理工具 — SSH 模式专属

支持 action=create|list|cancel：
- create：按 interval 或 cron（受限子集）创建定时任务写入 schedules 表，
  复用 ScheduleEngine 的 compute_next_run 计算下次执行时间，并确保引擎已启动。
- list：列出所有启用的定时任务。
- cancel：按 schedule_id 删除定时任务。

仅复用已有调度引擎与数据库层，不改动其实现。
"""
import asyncio

from agent.tools import BaseTool, ToolResult, ToolMeta
import database as db
# 注意：compute_next_run / get_schedule_engine 在方法内惰性导入，
# 避免 builtin 工具 import 时与 agent.schedule_engine 形成循环依赖。

# S7-06：cancel 是删除用户手工配好的任务，声明成 destructive —— gate 在 prod 环境
# 按 args["action"] 命中该集合时把它当 dangerous（只接受用户预登记的 allowlist，
# 不让模型代批）。原先这里没声明，prod 的巡检 run 也能靠一次「始终允许」删任务。
meta = ToolMeta(risk_level="moderate", reversible=False, timeout=10.0,
                destructive_actions=frozenset({"cancel"}))

# cron 字段位置：min hour dom month dow
_CRON_MIN, _CRON_HOUR, _CRON_DOM, _CRON_MONTH, _CRON_DOW = range(5)

# 引擎 weekday 编码：0=周一 … 6=周日
_CRON_DOW_TO_WEEKDAY = {
    1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5,
    0: 6, 7: 6,  # 0 或 7 都映射到周日
}


def _is_digits(s: str) -> bool:
    return bool(s) and s.isdigit()


def _parse_int_field(s: str, lo: int, hi: int, label: str, errs: list[str]) -> int | None:
    """解析单字段纯数字并校检区间；返回 None 表示该字段本身不合法（已入 errs）。"""
    if not _is_digits(s):
        errs.append(f"{label} 必须是数字或 '*'，收到 {s!r}")
        return None
    v = int(s)
    if not (lo <= v <= hi):
        errs.append(f"{label} 越界（允许 {lo}-{hi}），收到 {v}")
        return None
    return v


def _parse_cron(cron: str) -> tuple[dict, str | None]:
    """解析受限 cron 子集 → (schedule 数据, 错误信息)。不支持则返回 (None, 原因)。"""
    fields = cron.split()
    if len(fields) != 5:
        return None, (
            f"cron 表达式必须是 5 个字段（min hour dom month dow），收到 {len(fields)} 个："
            f" {cron!r}。支持的格式见下。"
        )
    min_s, hour_s, dom_s, month_s, dow_s = fields

    # 拒绝任何 step / range / list / 具名形式（*/N、1-5、1,2、月份名称）以及非通配的 month
    unsupported_markers = ("/", ",", "-")
    for s in fields:
        if any(m in s for m in unsupported_markers):
            return None, (
                f"cron 表达式 {cron!r} 使用了步长/范围/列表形式（*/N、1-5、1,2），"
                "当前暂不支持，请改用以下单一形式：\n"
                "  * * * * *（每天 00:00）、min hour * * *（每天）、\n"
                "  min hour * * dow（每周，0-7）、min hour dom * *（每月）"
            )
    if month_s != "*":
        return None, (
            f"cron 表达式 {cron!r} 的 month 字段须为 '*'，收到 {month_s!r}，暂不支持按月指定月份。"
        )

    errs: list[str] = []

    # 通配 daily 00:00
    if all(s == "*" for s in fields):
        return {"schedule_type": "daily", "time_hour": 0, "time_minute": 0,
                "weekday": 0, "day_of_month": 1}, None

    # min hour * * *（daily）
    if dom_s == "*" and dow_s == "*":
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        return {"schedule_type": "daily", "time_hour": h, "time_minute": m,
                "weekday": 0, "day_of_month": 1}, None

    # min hour dom * *（monthly，dom 为单个数字；dom='*' 的已全部被上面 daily 分支接走）
    if dow_s == "*" and month_s == "*":
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        # 上限取 28 而不是 cron 的 31：/schedules 的 _validate_fields 按 merged 校验，
        # 存 29-31 的行会让 PATCH（含界面上的勾选启停）恒返回 400，任务埋进去就改不动。
        d = _parse_int_field(dom_s, 1, 28, "dom(day_of_month)", errs)
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        return {"schedule_type": "monthly", "day_of_month": d, "time_hour": h, "time_minute": m,
                "weekday": 0}, None

    # min hour * * dow（weekly，dow 为单个数字 0-7）
    if dom_s == "*" and month_s == "*":
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        if not _is_digits(dow_s) or not (0 <= int(dow_s) <= 7):
            errs.append(f"dow 必须是 0-7 的数字，收到 {dow_s!r}")
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        weekday = _CRON_DOW_TO_WEEKDAY[int(dow_s)]
        return {"schedule_type": "weekly", "weekday": weekday, "time_hour": h, "time_minute": m,
                "day_of_month": 1}, None

    return None, f"cron 表达式 {cron!r} 无法识别，请使用以下受支持的格式之一：\n" \
        "  * * * * *（每天 00:00）、min hour * * *（每天）、\n" \
        "  min hour * * dow（每周，dow=0-7，0/7 为周日，1=周一…6=周六）、\n" \
        "  min hour dom * *（每月，dom=1-28）；不支持 */N、1-5、1,2 及按月指定月份。"


def _default_name(mode: str, parsed: dict) -> str:
    st = parsed.get("schedule_type") or "interval"
    if st == "interval":
        return f"每 {parsed.get('interval_minutes') or 60} 分钟"
    if st == "daily":
        return f"每天 {parsed.get('time_hour')}:{parsed.get('time_minute'):02d}"
    if st == "weekly":
        wd = parsed.get("weekday")
        return f"每周 周{['一','二','三','四','五','六','日'][wd]} {parsed.get('time_hour')}:{parsed.get('time_minute'):02d}"
    if st == "monthly":
        return f"每月 {parsed.get('day_of_month')} 日 {parsed.get('time_hour')}:{parsed.get('time_minute'):02d}"
    return "定时任务"


async def _resolve_target_host(raw) -> tuple[str, str, str]:
    """把模型给的目标主机写法归一化成连接配置 id（S7-09）。

    返回 `(id, 说明, 错误)`。三个要点：

    1. id 为空串是**合法档位**（只做模型侧检查、不建 SSH 会话），但必须在结果里写明
       —— 原先 `target_host` 根本不在 schema 里，模型无从传，于是「让 Agent 每天巡检
       web-01」建出来的任务天天跑、天天「已完成」、天天什么都没查（沉默降级，与 S7-02
       的无会话路径拼成完整一条假动作）。
    2. 主进程是唯一知道有哪些连接配置的地方，所以校验走桥；桥不可达/读不到列表时
       **按原值收下并标注「未校验」**，不拿探活失败挡死建任务。
    3. 命中不唯一或查不到时报错并列出可用目标 —— 这就是模型的发现路径，不需要再开
       一个 list_hosts 动作。
    """
    value = (raw or "").strip() if isinstance(raw, str) else ""
    if not value:
        return "", "未指定 target_host：本任务不会建立 SSH 会话，也不会执行任何远程命令。", ""

    from agent.electron_bridge import ElectronBridge

    bridge = ElectronBridge()
    note_prefix = ""
    conns: list[dict] = []
    if bridge.is_available():
        try:
            conns = await bridge.list_connections()
        except Exception as e:  # 桥断了/主进程拒绝：降级为「未校验」
            note_prefix = f"桥当前不可达（{type(e).__name__}），target_host 未校验；"
    else:
        note_prefix = "桥当前不可用，target_host 未校验；"

    if conns:
        by_id = {str(c.get("id") or ""): c for c in conns}
        if value in by_id:
            c = by_id[value]
            return value, f"目标主机：{c.get('name')} ({c.get('username')}@{c.get('host')})。", ""
        matches = [
            c for c in conns
            if value in (str(c.get("name") or ""), str(c.get("host") or ""),
                         f"{c.get('username')}@{c.get('host')}")
        ]
        if len(matches) == 1:
            mid = str(matches[0].get("id"))
            return mid, f"已把 {value!r} 解析为连接配置 id={mid}。", ""
        if len(matches) > 1:
            opts = "、".join(f"{c.get('id')}({c.get('name')})" for c in matches)
            return "", "", f"target_host {value!r} 命中 {len(matches)} 条连接配置，请改用具体的 id：{opts}"
        opts = "、".join(
            f"{c.get('id')}({c.get('name')} {c.get('username')}@{c.get('host')})" for c in conns
        )
        return "", "", f"找不到目标主机 {value!r}。可用连接配置：{opts}"

    return value, f"{note_prefix}按原值 {value!r} 落库，运行时会以它作为连接配置 id。", ""


class ScheduleOpTool(BaseTool):
    name = "schedule_op"
    description = (
        "管理定时任务（action=create|list|cancel）。create 支持每天/每周/每月定时"
        "与固定分钟间隔；list 列出启用任务；cancel 按 schedule_id 取消。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel"],
                "description": "操作类型：create 创建、list 列出、cancel 取消",
            },
            "mode": {
                "type": "string",
                "enum": ["interval", "cron"],
                "default": "interval",
                "description": "定时模式：interval=分钟间隔，cron=5 字段 cron 表达式（受限子集）",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "interval 模式的间隔分钟数（默认 60，最小 1）",
            },
            "cron": {
                "type": "string",
                "description": "5 字段 cron 表达式。仅支持：* * * * *（每天00:00）；"
                               "min hour * * *（每天）；min hour * * dow（每周，dow=0-7，0/7=周日，1=周一）；"
                               "min hour dom * *（每月，dom=1-28）。",
            },
            "task": {
                "type": "string",
                "description": "未来无头运行的运维任务提示词（写入 task_prompt）",
            },
            "command": {
                "type": "string",
                "description": "直接 shell 命令；提供时会前置写入 task_prompt 由未来运行执行",
            },
            "schedule_id": {
                "type": "string",
                "description": "cancel 时需提供的定时任务 ID",
            },
            "name": {
                "type": "string",
                "description": "任务名称（可选，默认按规则派生）",
            },
            "target_host": {
                "type": "string",
                "description": "目标主机：SSH 连接配置的 id，也可用唯一的连接名或 "
                               "host / username@host 指代（会自动解析成 id）。"
                               "留空 = 该任务不建立 SSH 会话、不执行任何远程命令。"
                               "写错时结果会列出可用目标主机。",
            },
        },
        "required": ["action"],
    }
    meta = meta

    # create 的 command 是模型自由文本（拼进 task_prompt 交未来的无头 run 执行），
    # 按 shell_exec 同口径判定 — 含「未入只读白名单即审批」的默认策略。
    bound_command_is_free_form = True

    @classmethod
    def bound_command(cls, args: dict) -> str | None:
        """入库前先判一次：定时任务是"现在埋雷、未来自动引爆"，推迟到那次无头 run
        才拒等于让 hardline 命令在库里躺着、等某个凌晨自己跑起来。

        S7-06：会改动/删除调度的 action 必须给出自己的裁决标的。原先只有
        `create` 带 command 时返回值，`cancel`（以及只用 task_prompt 的 create）
        一律 None ⇒ 授权键退回**裸工具名**（gate 的 `command_for_call` 拿不到
        命令就是这个形态），用户对一张卡片点「始终允许」，永久放行的是该工具的
        全部 action，包括删掉手工配好的任务。headless run 用同一份 registry，
        于是无人值守的定时 run 也能增删 schedules。键粒度收到 action
        （cancel 再收到具体 id）之后，「批的是查询、拿到的是删除」这条路就断了。

        `list` **刻意不返回标的**：它既不改动也不删除任何东西，风险判据应当来自
        ToolMeta（moderate，仍需会话授权）。一旦给它编一个 "schedule_op list"，
        这串就会被当成命令送进内容分类器，得到「unknown command, default
        approval」—— 判据从 ToolMeta 掉进命令分类兜底，审计里还可能被记成
        内容级阻断（钉住这条口径的是
        tests/test_permission_gate_content_bound_tools.py::test_schedule_op_non_create_falls_back_to_tool_level
        与 tests/test_ops_content_classification.py::test_schedule_op_non_create_has_no_bound_command）。
        """
        action = (args.get("action") or "").strip()
        if action == "create":
            cmd = args.get("command")
            if isinstance(cmd, str) and cmd.strip():
                return cmd
            # 只给 task_prompt 的 create 同样要绑：那才是未来真正被执行的东西
            prompt = (args.get("task") or "").strip()
            return f"schedule_op create: {prompt[:400]}" if prompt else None
        if action == "cancel":
            sid = str(args.get("schedule_id") or "").strip()
            return f"schedule_op cancel: {sid}" if sid else None
        return None

    async def execute(self, context, action, mode="interval", interval_minutes=None,
                      cron=None, task=None, command=None, schedule_id=None, name=None,
                      target_host=None):
        action = (action or "").strip()
        if action not in ("create", "list", "cancel"):
            return ToolResult(False, "", f"action 必须是 create|list|cancel，收到 {action!r}")

        if action == "create":
            return await self._create(mode, interval_minutes, cron, task, command, name,
                                      target_host)
        if action == "list":
            return await self._list()
        return await self._cancel(schedule_id)

    # ── create ──
    async def _create(self, mode, interval_minutes, cron, task, command, name,
                      target_host=None):
        mode = (mode or "interval").strip()
        if mode not in ("interval", "cron"):
            return ToolResult(False, "", f"mode 必须是 interval|cron，收到 {mode!r}")

        if mode == "interval":
            # 与 POST /schedules 的 Field(ge=1, le=525600) 同一口径：这里原先只做
            # max(1, int(x or 60))，非数字字符串直接 ValueError 冒出 _create（不在下面的
            # try 内），而 10**9 这种荒谬间隔能一路写进库。
            raw = 60 if interval_minutes is None else interval_minutes
            try:
                minutes = int(str(raw).strip())
            except (TypeError, ValueError):
                return ToolResult(False, "", f"interval_minutes 需为整数分钟，收到 {interval_minutes!r}")
            if not 1 <= minutes <= 525600:
                return ToolResult(False, "", f"interval_minutes 需在 1-525600（约 365 天）之间，收到 {minutes}")
            parsed = {"schedule_type": "interval", "interval_minutes": minutes}
        else:
            if not cron or not cron.strip():
                return ToolResult(False, "", "mode=cron 时必须提供非空的 cron 表达式")
            parsed, err = _parse_cron(cron.strip())
            if err:
                return ToolResult(False, "", f"cron 表达式不受支持/无效: {err}")

        task_prompt = (task or "").strip()
        if command and command.strip():
            task_prompt = f"{task_prompt}\n请执行以下命令并把结果写进结论：\n{command}".strip()

        task_name = (name or "").strip() or _default_name(mode, parsed)

        host_id, host_note, host_err = await _resolve_target_host(target_host)
        if host_err:
            return ToolResult(False, "", host_err)

        data = {
            "name": task_name,
            "schedule_type": parsed["schedule_type"],
            # cron 派生的行不落 interval_minutes：留 0 的话，用户日后在界面上把类型
            # 改成 interval 会撞上 POST/PATCH 的 ge=1 校验（422），而 UI 建行的默认是 60
            "interval_minutes": parsed.get("interval_minutes", 60),
            "time_hour": parsed.get("time_hour", 9),
            "time_minute": parsed.get("time_minute", 0),
            # weekday 没有「不适用」哨兵：compute_next_run 直接 int() 后与
            # date.weekday()（0-6）比较，存 -1 等于埋一条永远不触发的周任务
            "weekday": parsed.get("weekday", 0),
            "day_of_month": parsed.get("day_of_month", 1),
            # S7-09：解析后的连接配置 id（空串 = 本任务不建 SSH 会话）。原先这里是
            # 硬编码 ""，而 schema 又没有 target_host 参数 —— 模型无从传，AI 排的班
            # 永远不带目标主机。
            "target_host": host_id,
            "task_prompt": task_prompt,
            "enabled": 1,
        }

        from agent.schedule_engine import compute_next_run
        try:
            next_at = await asyncio.to_thread(compute_next_run, data)
            # 审查修复：next_run_at 必须与插入同事务落定（并入 create_schedule 的 data，
            # 与 REST 路由 routes/schedules.py:88 一致）。原实现先插 NULL 再在下一跳
            # set_schedule_next_run，两跳之间 _tick_once 把 NULL 视为到期 → 刚建的
            # "每天 09:00" 任务被立即触发一次。
            created = await asyncio.to_thread(db.create_schedule, {**data, "next_run_at": next_at})
            sid = created["id"]
            # 惰性导入避免 import 期循环依赖；确保后台引擎已启动以触发该任务
            from agent.schedule_engine import get_schedule_engine
            get_schedule_engine().ensure_started()
        except Exception as e:
            return ToolResult(False, "", f"创建定时任务失败: {type(e).__name__}: {e}")

        t = parsed["schedule_type"]
        return ToolResult(
            True,
            f"已创建定时任务 schedule_id={sid}\n名称: {task_name}\n类型: {t}, 下次执行: {next_at}\n"
            f"{host_note}",
        )

    # ── list ──
    async def _list(self):
        try:
            rows = await asyncio.to_thread(db.list_enabled_schedules)
        except Exception as e:
            return ToolResult(False, "", f"列出定时任务失败: {type(e).__name__}: {e}")

        if not rows:
            # 成功态下驱动只把 output 喂回模型（driver.py: result.success → result.output），
            # 原先提示写在 error 里、output 为空，模型看到的是空结果。
            return ToolResult(True, "暂无启用的定时任务")
        lines = ["启用中的定时任务："]
        for r in rows:
            t = (r.get("task_prompt") or "").strip().splitlines()
            first = t[0][:40] if t else ""
            st = r.get("schedule_type") or "interval"
            lines.append(f"{r.get('id')} | {r.get('name') or ''} | {st} | {first}")
        return ToolResult(True, "\n".join(lines))


    # ── cancel ──
    async def _cancel(self, schedule_id):
        if not schedule_id or not str(schedule_id).strip():
            return ToolResult(False, "", "cancel 需要提供 schedule_id 参数")
        sid = str(schedule_id).strip()
        try:
            # 与 REST DELETE 同一实现：取消在途执行体 + 落定 running 行再删
            from agent.schedule_engine import get_schedule_engine
            ok = await get_schedule_engine().remove_schedule(sid)
        except Exception as e:
            return ToolResult(False, "", f"取消定时任务失败: {type(e).__name__}: {e}")
        if ok:
            return ToolResult(True, f"已取消定时任务 {sid}")
        return ToolResult(False, "", f"未找到定时任务 {sid}，无法取消")