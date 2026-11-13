"""应用设置 key-value CRUD 与策略读取 — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
迁移期两份实现必须逐字一致（AST parity，参照 tests/test_db_schedules_parity.py 的口径）。
get_db_path 是连接层状态访问器（读模块级 _db_path），留待连接层收口时处理，未随本模块迁移。
unprotect_secret 属凭据信封域（计划中的 db/_secrets.py），暂从 database 导入；该域迁移后改从 db/_secrets 引用。
"""
from typing import Optional

from db._conn import _get_conn
from db._util import _now
from database import unprotect_secret

__all__ = [
    "get_app_setting",
    "set_app_setting",
    "update_app_settings_bulk",
    "list_app_settings",
    "count_rows",
    "get_http_policy",
    "diag_counts",
    "get_conv_retention",
    "get_permission_policy",
    "bool_app_setting",
    "get_memory_settings",
]


def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """读取单个应用设置。

    带凭据信封（`dpapi:v1:` 前缀，见 protect_secret 区块注释）的值在这里统一
    还原成明文 —— 消费方（web_tools 的 Tavily 调用、诊断面板等）不感知信封，
    见到的永远是可用的明文或未设置的空值。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        value = row["value"] if row else default
        return unprotect_secret(value) if value else value
    finally:
        conn.close()


def set_app_setting(key: str, value: str) -> None:
    """写入/更新应用设置"""
    now = _now()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
            (key, value, now, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def update_app_settings_bulk(settings: dict[str, str]) -> None:
    """单事务批量写入多个应用设置。

    审查R2修复：多键设置（审批策略 / 出站 HTTP 策略）此前逐条 set_app_setting
    各自 commit，中途一条失败 = 库里一半新一半旧，而路由随后仍把内存策略按"全部
    新值"应用 —— 两边各说各话。本 helper 把整批写进同一事务，任何一条失败整体
    回滚并抛出，由调用方以 500 结束、内存策略保持旧值。
    """
    now = _now()
    conn = _get_conn()
    try:
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
                (key, value, now, value, now),
            )
        conn.commit()
    except Exception:
        # 线程本地池化连接的 close 是 no-op，不显式 rollback 会把半截事务
        # 留在该线程上，污染后续读写
        conn.rollback()
        raise
    finally:
        conn.close()


def list_app_settings() -> dict:
    """全部应用设置键值（诊断面板用）。只读，脱敏由调用方负责。"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_settings ORDER BY key"
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


# 诊断面板的行数统计白名单：拼进 SQL 的表名必须来自这里，不接受任意入参。
# 这是 count_rows 里 f-string 的唯一放行守卫（fail-closed）：不在元组内的
# 表名直接返回 None，标识符不可能带进 SQL —— 数据值本就没有插值位。
_DIAG_COUNT_TABLES = ("conversations", "messages", "agent_runs", "models_config", "skills")


def count_rows(table: str) -> Optional[int]:
    if table not in _DIAG_COUNT_TABLES:
        return None
    conn = _get_conn()
    try:
        # {table} 已被上方 _DIAG_COUNT_TABLES 白名单收窄（见其注释），剩余
        # 风险只剩「白名单值本身写坏」，字面量元组由 review 兜底，无需再断言
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    except Exception:
        return None
    finally:
        conn.close()


def get_http_policy() -> dict:
    """出站 HTTP 策略（G4/G5）。唯一读取点：netconfig 的启动/重载；进程内实际
    生效值由 netconfig 缓存，routes/settings.py 的 GET/PUT 读的是那份缓存。

    区间与写入侧 Field 同口径：超时 0..600（0 = 不覆盖调用点默认），重试 0..3。
    代理在库里可能带 userinfo，读取时原样返回，脱敏是展示层的责任。

    retry_set / timeout_set / proxy_set 把"库里有没有一行合法值"如实带出去：
    netconfig 靠它决定 env 兜底该不该让位（缺行才用 env，用户写过就以 DB 为准，
    显式写的 0 也算写过）。
    这里不读 env —— env 优先级归 netconfig 单管，两边各读一份会让规则在两个文件里
    各演化一遍。脏行/越界行按默认读数显示但不算"用户设定"，免得一个改坏的值
    永久锁掉运维注入。
    """
    timeout, timeout_set = is_int_app_setting_set(
        "http.timeout_seconds", 0, low=0, high=600)
    retries, retry_set = is_int_app_setting_set(
        "http.retry_count", 1, low=0, high=3)
    return {
        # 代理**故意没有** proxy_set。本仓口径是「库里非空才压过 env」，空串行与缺行
        # 同等对待（round 19 登记、round 22 用两条测试钉住）。切片审查 S0-01 对此有
        # 异议并要求产品裁决 —— 裁决之前不要在这里加「行存在即权威」的判据，
        # 完整来龙去脉见 netconfig.load_policy_from_db 的注释。
        "proxy_url": (get_app_setting("http.proxy_url") or "").strip(),
        "timeout_seconds": timeout,
        "retry_count": retries,
        "timeout_set": timeout_set,
        "retry_set": retry_set,
    }


def diag_counts() -> dict:
    """诊断面板的行数快照（G10）。表名固定取自白名单，不接受外部入参。"""
    return {t: count_rows(t) for t in _DIAG_COUNT_TABLES}


def _int_app_setting(key: str, default: int, low: Optional[int] = None, high: Optional[int] = None) -> int:
    """读取整型 app_setting：脏值/越界值回默认值而不是抛 ValueError。

    写入侧（routes/settings.py 的 Field 区间）只管得住走接口的写：升级前的旧行、
    直接改库、或校验收紧前落下的越界值仍会留在表里。读取侧不兜住的话，一个脏行
    就让 GET /settings/* 整条 500、面板永远打不开，用户连改回来的入口都没有。

    low/high 传入时按写入区间一并校验：越界同样回默认值（不钳到边界——库里那行是
    什么用户意图不可知，默认值是唯一说得过去的读数）。
    """
    value, explicit = is_int_app_setting_set(key, default, low=low, high=high)
    return value if explicit else default


def is_int_app_setting_set(key: str, default: int,
                           low: Optional[int] = None,
                           high: Optional[int] = None) -> tuple[int, bool]:
    """返回 (读数, 这一行是不是用户写下的合法值)。

    "合法"= 存在、非空、能按 int 解析、且不越 low/high。脏行与越界行都算 False：
    调用方（netconfig 的 env 兜底）据此决定该不该让 env 说话 —— 一个手工改坏的
    值不该永久锁掉运维注入。读数按 _int_app_setting 的口径回默认，不钳边界。
    """
    raw = get_app_setting(key)
    if raw is None or not str(raw).strip():
        return default, False
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, False
    if low is not None and value < low:
        return default, False
    if high is not None and value > high:
        return default, False
    return value, True


def get_conv_retention() -> dict:
    """会话保留清理设置（默认关闭）。

    唯一读取点：routes/settings.py 的 GET/PUT 与 routes/agent.py 的 run 前置清理
    原先各抄一份，key 或默认值一改就会两边漂移。
    """
    return {
        "enabled": bool_app_setting("conv_retention.enabled", False),
        # 区间与写入侧 Field 同口径：这条设置打开后是真删用户会话的，脏行/越界行
        # 必须回默认而不是原样放出去（keep_count 会进 DELETE ... rn > ?）。
        "keep_count": _int_app_setting("conv_retention.keep_count", 1000, low=0, high=100000),
    }


def get_permission_policy() -> dict:
    """审批授权策略（E3 + 审批策略两新字段）。唯一读取点：routes/settings.py 的
    GET/PUT 与 main.py 启动回填。

    allowlist_ttl_hours=0（默认）= 永不过期，即引入 TTL 之前 allowlist 的原始语义 ——
    默认值不能选"7 天"，那会让升级当天既有 prod 预登记条目凭空开始倒计时。
    auto_approve_enabled=True（默认）= 设置面板侧不额外拦截；env kill switch
    仍在调用处优先压过它（auto_approve.auto_approve_effective）。
    approval_fallback_s=600（默认）= 审批兜底等待秒数，0 = 永久等待。
    区间与写入侧 Field 同口径（0..8760 小时 / 0..86400 秒）。
    """
    return {
        "allowlist_ttl_hours": _int_app_setting(
            "permission.allowlist_ttl_hours", 0, low=0, high=8760),
        "auto_approve_enabled": bool_app_setting(
            "permission.auto_approve_enabled", True),
        "approval_fallback_s": _int_app_setting(
            "permission.approval_fallback_s", 600, low=0, high=86400),
    }


def bool_app_setting(key: str, default: bool) -> bool:
    """布尔 app_setting：只有字面 "true" 才为 True，脏值回默认。

    全库唯一的布尔设置解析口径 —— 此前 `== "true"`、`.lower() == "true"` 与
    各自的局部函数写了好几份，同一个 "1" 在不同设置上含义不同。写入侧
    （routes/settings.py）一律 `"true" if v else "false"`，所以这里认得比它多
    没有意义，只会把历史脏值洗成意外打开的开关。
    """
    raw = get_app_setting(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip() == "true"


def _float_app_setting(key: str, default: float,
                       low: Optional[float] = None, high: Optional[float] = None) -> float:
    """浮点 app_setting，语义同 _int_app_setting（脏值/越界回默认，不钳边界）"""
    raw = get_app_setting(key)
    try:
        value = float(raw) if raw is not None and str(raw).strip() else default
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN 字面量：任何区间比较都为 False，会溜过 low/high 检查
        return default
    if low is not None and value < low:
        return default
    if high is not None and value > high:
        return default
    return value


def get_memory_settings() -> dict:
    """读取记忆相关设置，返回带默认值的 dict

    max_items 的区间就是写入侧的 Field 区间（10..500），越界读数不能原样放出去：
    这个值喂到 consciousness_tick → mstore.trim_pools(max_items)，其 SQL 是
    `LIMIT -1 OFFSET ?` —— max_items=0 等于 OFFSET 0 无 LIMIT，把每个 kind 下
    全部 active 且未 pin 的记忆一次 retire 掉。没有面板能改回这行，用户只能
    眼看着经验被清光。

    两个 token 预算分账：注入记忆（检索到的历史经验）与画像块（常驻已知事实）各有
    上限，一方变长不会把另一方挤没 —— 写在一处会让"改大画像"悄悄削减"检索召回"。
    """
    return {
        "enabled": bool_app_setting("memory.enabled", True),
        "max_items": _int_app_setting("memory.max_items", 100, low=10, high=500),
        "inject_budget_tokens": _int_app_setting(
            "memory.inject_budget_tokens", 1500, low=200, high=8000),
        "blocks_budget_tokens": _int_app_setting(
            "memory.blocks_budget_tokens", 1200, low=200, high=8000),
        "search_timeout_s": _float_app_setting("memory.search_timeout_s", 8.0, low=0.5, high=30.0),
        "reconcile_llm_enabled": bool_app_setting("memory.reconcile_llm_enabled", True),
    }
