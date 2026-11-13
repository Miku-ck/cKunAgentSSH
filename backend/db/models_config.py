"""模型配置 CRUD — 从 database.py 原样复制的领域模块（P0 阶段 3）。

公共入口仍是 backend/database.py（再导出）；本模块供新代码按需引用。
迁移期两份实现必须逐字一致（AST parity，参照 tests/test_db_schedules_parity.py 的口径）。
_insert_seed_model / seed_models_async 属启动播种链路，暂留 database.py。
"""
from typing import Optional

from db._conn import _get_conn
from db._secrets import protect_secret, unprotect_secret
from db._util import _mask_api_key, _new_id, _now

__all__ = [
    "list_models_config",
    "get_model_config",
    "create_model_config",
    "update_model_config",
    "delete_model_config",
    "set_default_model",
    "get_default_model",
]


# models_config 全列（三处查询共用）。此前各处各写一份字面量，
# get_default_model 那份漏了 reasoning_tokens —— 读默认模型的调用方拿到 None，
# 思考预算退化为「不限制」。
_MC_COLS = (
    "id, name, model_id, provider, base_url, api_key, "
    "supports_thinking, supports_image, supports_tool_calling, "
    "is_default, is_default_ssh, "
    "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at"
)


def _resolve_model_pk(ref: str) -> Optional[str]:
    """把「行主键 id」或「LLM 模型名（model_id 列）」统一解析成行主键；解析不到返回 None。

    读路径（get_model_config）一直认两种键 —— Agent 后处理与默认模型兜底都传模型名；
    写路径此前只认行主键，于是 PATCH default 传模型名时：存在性检查过 → 清 others 的
    UPDATE 按 id != 名字匹配到**全部**行（真默认被清掉）→ 置一的 UPDATE 匹配 0 行 →
    结果是 0 个默认模型，而响应 200。读写必须共用一个解析器，否则每加一个写入口
    就要再踩一遍（round 14 已登记）。
    """
    if not ref:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM models_config WHERE id = ?", (ref,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM models_config WHERE model_id = ? ORDER BY rowid LIMIT 1",
                (ref,),
            ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def list_models_config() -> list[dict]:
    """获取所有模型配置（按 sort_order 正序，api_key 已脱敏）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            f"SELECT {_MC_COLS} "
            "FROM models_config ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
        result = [dict(r) for r in rows]
        for item in result:
            # 信封值先还原再掩码：掩码面向前端的口径是「明文的前 4 位」，不是
            # 信封密文的前 4 位（密文每次加封都不同，掩出来就是噪声）
            item["api_key"] = _mask_api_key(unprotect_secret(item.get("api_key") or ""))
        return result
    finally:
        conn.close()


def get_model_config(model_id: str, mask_key: bool = True) -> Optional[dict]:
    """根据 id 获取模型配置（mask_key=True 时 api_key 已脱敏）

    调用方可能传入行主键 id（model-<uuid>）或 LLM 模型名（model_id 列，如
    qwen3.8-27b）— Agent 后处理原样传模型名、chat/agent 的默认模型兜底也优先取
    模型名。两种键的解析规则**只存在于 `_resolve_model_pk` 一处**（S10-22）：
    此前这里抄了第二份「先按 id、落空再按 model_id 回退」，读写两侧各种一份，
    以后调整匹配规则（例如重名取舍）就会只改一边而留下会静默失效的副本。
    """
    if not model_id:
        return None
    pk = _resolve_model_pk(model_id)
    if pk is None:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_MC_COLS} FROM models_config WHERE id = ?",
            (pk,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # 先还原凭据信封（unprotect_secret 对旧明文行是恒等变换），掩码与
        # mask_key=False 的内部消费方（llm_adapter 等）拿到的都是明文
        _raw_key = result.get("api_key")
        if _raw_key is not None:
            _raw_key = unprotect_secret(_raw_key)
        result["api_key"] = _raw_key
        if mask_key:
            result["api_key"] = _mask_api_key(_raw_key)
        return result
    finally:
        conn.close()


def create_model_config(
    name: str,
    base_url: str,
    api_key: str = "",
    model_id: str = "",
    provider: str = "custom",
    supports_thinking: bool = False,
    supports_image: bool = False,
    supports_tool_calling: bool = False,
    temperature: float = 0.7,
    # max_tokens 是「上下文窗口」列（与 models_config 建表 DEFAULT 16384 一致），
    # 不是输出预算 —— 输出预算由 max_output_tokens 管。窗口若默认 4096，
    # safe_max_tokens 会把输出压到 2048、思考模式下压到 256 下限。
    max_tokens: int = 16384,
    max_output_tokens: int = 4096,
    top_p: float = 0.9,
    is_default_ssh: bool = False,
    reasoning_tokens: int = 8192,
) -> dict:
    """创建模型配置，返回完整记录"""
    # 去除首尾空格：粘贴时可能带入换行/空白，导致上游 API 校验失败（401）
    name = (name or "").strip()
    base_url = (base_url or "").strip()
    api_key = protect_secret((api_key or "").strip())
    model_id = (model_id or "").strip()
    mid = _new_id()
    now = _now()
    conn = _get_conn()
    try:
        # 计算 sort_order（已有最大 + 1）
        row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS mx FROM models_config").fetchone()
        next_order = row["mx"] + 1
        conn.execute(
            "INSERT INTO models_config "
            "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
            "is_default, is_default_ssh, "
            "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid, name, model_id, provider, base_url, api_key,
                1 if supports_thinking else 0,
                1 if supports_image else 0,
                1 if supports_tool_calling else 0,
                1 if is_default_ssh else 0,
                temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, next_order, now, now,
            ),
        )
        # 如果新模型被设为默认，需清除其他模型的默认标记
        if is_default_ssh:
            conn.execute("UPDATE models_config SET is_default_ssh = 0 WHERE id != ?", (mid,))
        conn.commit()
        return get_model_config(mid)  # type: ignore
    finally:
        conn.close()


def update_model_config(config_id: str, **kwargs) -> Optional[dict]:
    """更新模型配置（支持部分字段）

    config_id 认行主键 id 也认模型名（与 get_model_config / set_default_model 同一
    解析器），kwargs 里的 model_id 字段与此互不冲突。
    """
    pk = _resolve_model_pk(config_id)
    if pk is None:
        return None
    allowed = {
        "name", "model_id", "provider", "base_url", "api_key",
        "supports_thinking", "supports_image", "supports_tool_calling",
        "temperature", "max_tokens", "max_output_tokens", "reasoning_tokens", "top_p", "sort_order",
        "is_default", "is_default_ssh",
    }
    sets = []
    params = []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            # api_key 空字符串表示「保留原值」，跳过更新
            if k == "api_key" and v == "":
                continue
            # B-10 修复：前端回传的掩码值（含 "****"）不是真密钥 —
            # 原实现会把掩码写回 DB 覆盖真实 api_key，导致模型 401
            if k == "api_key" and "****" in str(v):
                continue
            # api_key 落库前加凭据信封（见 protect_secret；历史明文行在下次
            # 保存时于此处完成信封升级）
            if k == "api_key":
                v = protect_secret(str(v))
            sets.append(f"{k} = ?")
            params.append(1 if isinstance(v, bool) else v)
    # 记录是否存在（优先判断，避免 rowcount 0 的误判）
    existing = get_model_config(pk)
    if not existing:
        return None
    if not sets:
        return existing
    now = _now()
    sets.append("updated_at = ?")
    params.append(now)
    # pk 必须放在最后，对应 SQL 中 WHERE id = ? 的位置
    params.append(pk)
    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE models_config SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        # 默认标记是唯一标记：置了某一档就得清掉同档的其它行，与 set_default_model
        # 同一个原语，免得两处对"唯一"的理解各写各的
        for col, key in (("is_default", "is_default"), ("is_default_ssh", "is_default_ssh")):
            if kwargs.get(key):
                conn.execute(
                    f"UPDATE models_config SET {col} = 0 WHERE id != ?", (pk,))
        conn.commit()
        # rowcount 0 不代表记录不存在——SQLite 在值未变化时不计入 rowcount
        return get_model_config(pk)
    finally:
        conn.close()


def delete_model_config(model_id: str) -> bool:
    """删除模型配置（默认模型不可删除，任一档都算）"""
    pk = _resolve_model_pk(model_id)
    if pk is None:
        return False
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT is_default, is_default_ssh FROM models_config WHERE id = ?",
            (pk,),
        ).fetchone()
        if row and (row["is_default"] or row["is_default_ssh"]):
            raise ValueError("Cannot delete a model that is set as default")
        cur = conn.execute("DELETE FROM models_config WHERE id = ?", (pk,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# 默认档位 → 列名。mode=None 沿用历史口径（不传 = 全局档），"ssh" 是 routes 里
# SSH_MODE 的取值；显式收窄成这张表，别让"任何非 ssh 的值"都落进 SSH 列。
_DEFAULT_COLUMNS = {None: "is_default", "global": "is_default", "ssh": "is_default_ssh"}


def set_default_model(model_id: str, mode: Optional[str] = None) -> None:
    """设置默认模型。

    mode：None 或 "global" → 全局 is_default；"ssh" → SSH 模式默认。
    model_id 认行主键也认模型名（_resolve_model_pk）；解析不到抛 ValueError ——
    旧实现按名字传进来会"清掉真默认 + 目标匹配 0 行 + 静默成功"，是本函数的
    主要踩坑面。

    审查修复：原实现"全清 + 置一"为两条非原子 UPDATE，并发切换可产生
    0 个或 2 个默认模型（两条语句间交错）。改为单条条件 UPDATE
    （清 others 带 WHERE id != 目标后提交同一事务），消除中间状态。
    """
    if mode not in _DEFAULT_COLUMNS:
        raise ValueError(f"未知的默认档位 {mode!r}（可用：global / ssh）")
    pk = _resolve_model_pk(model_id)
    if pk is None:
        raise ValueError(f"模型不存在: {model_id}")
    col = _DEFAULT_COLUMNS[mode]
    conn = _get_conn()
    try:
        # 两条 UPDATE 置于同一隐式事务，最后一次性 commit（保持原子性）
        conn.execute(
            f"UPDATE models_config SET {col} = 0 WHERE id != ?", (pk,)
        )
        conn.execute(
            f"UPDATE models_config SET {col} = 1 WHERE id = ?", (pk,)
        )
        conn.commit()
    finally:
        conn.close()


def get_default_model(mode: Optional[str] = None, mask_key: bool = True) -> Optional[dict]:
    """获取当前默认模型配置。
    mode 为 None 时返回全局 is_default 模型；
    mode 为 'ssh' 时返回 SSH 模式的默认模型。
    mask_key=True（默认）时 api_key 脱敏，面向前端/只取 id 的场景；
    内部真正调用 LLM/embedding 的场景传 mask_key=False 获取真实 key。
    """
    conn = _get_conn()
    try:
        col = "is_default" if mode is None else "is_default_ssh"
        row = conn.execute(
            f"SELECT {_MC_COLS} FROM models_config WHERE {col} = 1 LIMIT 1"
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        # C1 修复：默认脱敏 api_key，避免明文密钥泄露到前端
        # 审查 M-15：内部需要真实 key 的调用方（embedding/LLM）传 mask_key=False，
        # 否则拿到 "EMPT****" 式脱敏值 → 401 → 静默降级伪 embedding → 记忆维度混存
        # 两个分支都先还原凭据信封（get_model_config 同款），再按 mask_key 决定回明文还是掩码
        _raw_key = result.get("api_key")
        if _raw_key is not None:
            _raw_key = unprotect_secret(_raw_key)
        result["api_key"] = _raw_key if not mask_key else _mask_api_key(_raw_key)
        return result
    finally:
        conn.close()
