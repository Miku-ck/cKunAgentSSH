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

meta = ToolMeta(risk_level="moderate", reversible=False, timeout=10.0, mode_scope=["ssh"])

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
                "interval_minutes": 0, "weekday": -1, "day_of_month": 1}, None

    # min hour * * *（daily）
    if dom_s == "*" and dow_s == "*":
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        return {"schedule_type": "daily", "time_hour": h, "time_minute": m,
                "interval_minutes": 0, "weekday": -1, "day_of_month": 1}, None

    # min hour dom * *（monthly，dom 为单个数字）
    if dow_s == "*" and month_s == "*":
        if dom_s == "*":
            return None, f"cron 表达式 {cron!r} 格式无法识别（min/hour 已定但 dom 和 dow 均为 '*'）。"
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        d = _parse_int_field(dom_s, 1, 31, "dom(day_of_month)", errs)
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        return {"schedule_type": "monthly", "day_of_month": d, "time_hour": h, "time_minute": m,
                "interval_minutes": 0, "weekday": -1}, None

    # min hour * * dow（weekly，dow 为单个数字 0-7）
    if dom_s == "*" and month_s == "*":
        if dow_s == "*":
            return None, f"cron 表达式 {cron!r} 格式无法识别（min/hour 已定但 dom 和 dow 均为 '*'）。"
        m = _parse_int_field(min_s, 0, 59, "min", errs)
        h = _parse_int_field(hour_s, 0, 23, "hour", errs)
        if not _is_digits(dow_s) or not (0 <= int(dow_s) <= 7):
            errs.append(f"dow 必须是 0-7 的数字，收到 {dow_s!r}")
        if errs:
            return None, "cron 表达式无效: " + "\n".join(errs)
        weekday = _CRON_DOW_TO_WEEKDAY[int(dow_s)]
        return {"schedule_type": "weekly", "weekday": weekday, "time_hour": h, "time_minute": m,
                "interval_minutes": 0, "day_of_month": 1}, None

    return None, f"cron 表达式 {cron!r} 无法识别，请使用以下受支持的格式之一：\n" \
        "  * * * * *（每天 00:00）、min hour * * *（每天）、\n" \
        "  min hour * * dow（每周，dow=0-7，0/7 为周日，1=周一…6=周六）、\n" \
        "  min hour dom * *（每月，dom=1-31）；不支持 */N、1-5、1,2 及按月指定月份。"


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
                               "min hour dom * *（每月，dom=1-31）。",
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
        },
        "required": ["action"],
    }
    meta = meta

    async def execute(self, context, action, mode="interval", interval_minutes=None,
                      cron=None, task=None, command=None, schedule_id=None, name=None):
        action = (action or "").strip()
        if action not in ("create", "list", "cancel"):
            return ToolResult(False, "", f"action 必须是 create|list|cancel，收到 {action!r}")

        if action == "create":
            return await self._create(mode, interval_minutes, cron, task, command, name)
        if action == "list":
            return await self._list()
        return await self._cancel(schedule_id)

    # ── create ──
    async def _create(self, mode, interval_minutes, cron, task, command, name):
        mode = (mode or "interval").strip()
        if mode not in ("interval", "cron"):
            return ToolResult(False, "", f"mode 必须是 interval|cron，收到 {mode!r}")

        if mode == "interval":
            parsed = {"schedule_type": "interval",
                      "interval_minutes": max(1, int(interval_minutes or 60))}
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

        data = {
            "name": task_name,
            "schedule_type": parsed["schedule_type"],
            "interval_minutes": parsed.get("interval_minutes", 0),
            "time_hour": parsed.get("time_hour", 9),
            "time_minute": parsed.get("time_minute", 0),
            "weekday": parsed.get("weekday", 0),
            "day_of_month": parsed.get("day_of_month", 1),
            "target_host": "",
            "task_prompt": task_prompt,
            "enabled": 1,
        }

        from agent.schedule_engine import compute_next_run
        try:
            next_at = await asyncio.to_thread(compute_next_run, data)
            created = await asyncio.to_thread(db.create_schedule, dict(data))
            sid = created["id"]
            await asyncio.to_thread(db.set_schedule_next_run, sid, next_at)
            # 惰性导入避免 import 期循环依赖；确保后台引擎已启动以触发该任务
            from agent.schedule_engine import get_schedule_engine
            get_schedule_engine().ensure_started()
        except Exception as e:
            return ToolResult(False, "", f"创建定时任务失败: {type(e).__name__}: {e}")

        t = parsed["schedule_type"]
        return ToolResult(
            True,
            f"已创建定时任务 schedule_id={sid}\n名称: {task_name}\n类型: {t}, 下次执行: {next_at}",
        )

    # ── list ──
    async def _list(self):
        try:
            rows = await asyncio.to_thread(db.list_enabled_schedules)
        except Exception as e:
            return ToolResult(False, "", f"列出定时任务失败: {type(e).__name__}: {e}")

        if not rows:
            return ToolResult(True, "", "暂无启用的定时任务")
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
            ok = await asyncio.to_thread(db.delete_schedule, sid)
        except Exception as e:
            return ToolResult(False, "", f"取消定时任务失败: {type(e).__name__}: {e}")
        if ok:
            return ToolResult(True, f"已取消定时任务 {sid}")
        return ToolResult(False, "", f"未找到定时任务 {sid}，无法取消")