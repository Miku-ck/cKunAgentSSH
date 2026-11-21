"""首启模型种子（P2-3 从 database.py 拆出，函数体与入口签名不变）。"""
import asyncio
import logging
import sqlite3

from db import _secrets as _secrets_mod
from db import _util as _util_mod

logger = logging.getLogger(__name__)


async def seed_models_async(db_path: str) -> None:
    """异步模型种子 — 仅在 models_config 表为空且显式配置 VLLM_BASE_URL 时执行。

    审查修复（P2 E9）：_probe_first_model 内含同步 httpx.get（最长阻塞 3s×2=6s）。
    原实现从 async lifespan 直接调 init_db → _seed_models → _probe_first_model，
    6s 同步阻塞阻塞整个事件循环（所有 FastAPI 请求/后台任务/心跳全卡死）。
    改为独立异步入口：httpx 调用放 asyncio.to_thread 线程池，事件循环保持畅通。
    """
    # 调用侧解析：测试打桩 database._probe_first_model（test_model_seed_probe）
    from database import _probe_first_model

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM models_config").fetchone()
        if row["cnt"] > 0:
            return
        try:
            from config import settings
            base_url = (settings.vllm_base_url or "").strip()
            thinking_url = (settings.vllm_thinking_url or "").strip()
            api_key = settings.vllm_api_key or "EMPTY"
        except Exception as e:
            # 静默 return 会让"首启没有预填模型"变成一个没有答案的问题：
            # 用户只看到空模型列表，而真正的失败原因（配置导入炸了）无处可查。
            logger.warning("首启自动探测：读取 config.settings 失败，跳过种子模型: %s", e)
            return
        if not base_url:
            return

        now = _util_mod._now()
        # httpx.get 是同步阻塞调用，放线程池避免阻塞事件循环
        # 审查R2修复：两条种子各自独立 try + 即时 commit —— 原实现整批一条
        # commit，两条种子撞上同名模型（models_config.id 即模型名）抛
        # IntegrityError 时，连接关闭回滚把先一条已成功的种子连坐抹掉。
        # 现在单条冲突只 warning 跳过，不牵连另一条。
        #
        # 两条探测彼此独立（不同 endpoint），并发跑：最坏 3s+3s 压到最坏 3s。
        # return_exceptions=True 与上面「各自独立」同一取舍 —— 一条探测抛异常
        # 不该连坐另一条；写入仍按主/思考顺序逐条落地，保持 rowid 次序可预期。
        targets = [(base_url, False, True, 0, "默认")]
        if thinking_url and thinking_url.rstrip("/") != base_url.rstrip("/"):
            targets.append((thinking_url, True, False, 1, "思考"))
        probed = await asyncio.gather(
            *(asyncio.to_thread(_probe_first_model, url, api_key) for url, *_ in targets),
            return_exceptions=True,
        )
        for (url, supports_thinking, is_default, sort_order, label), model_id in zip(
            targets, probed
        ):
            if isinstance(model_id, BaseException):
                logger.warning("种子%s模型探测失败（跳过，不影响另一条种子）: %s: %s",
                               label, url, model_id)
                continue
            if not model_id:
                continue
            try:
                _insert_seed_model(
                    conn, model_id=model_id, base_url=url, api_key=api_key,
                    supports_thinking=supports_thinking, is_default=is_default,
                    sort_order=sort_order, now=now,
                )
                conn.commit()
                logger.info("自动探测种子%s模型: %s @ %s", label, model_id, url)
            except sqlite3.Error as e:
                conn.rollback()
                logger.warning("种子%s模型写入失败（跳过，不影响另一条种子）: %s @ %s: %s",
                               label, model_id, url, e)
    finally:
        conn.close()
def _insert_seed_model(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    base_url: str,
    api_key: str,
    supports_thinking: bool,
    is_default: bool,
    sort_order: int,
    now: str,
) -> None:
    """插入一条首启种子模型配置（固定保守默认参数，用户可在 UI 调整）"""
    # env 注入的种子 api_key 与 UI 写入同口径加信封（见 protect_secret 区块注释）
    api_key = _secrets_mod.protect_secret(api_key)
    conn.execute(
        "INSERT INTO models_config "
        "(id, name, model_id, provider, base_url, api_key, supports_thinking, supports_image, supports_tool_calling, "
        "is_default, is_default_ssh, "
        "temperature, max_tokens, max_output_tokens, reasoning_tokens, top_p, sort_order, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 0.7, 16384, 4096, 8192, 0.9, ?, ?, ?)",
        (
            model_id, model_id, model_id, "vllm",
            base_url, api_key, int(supports_thinking), int(is_default), int(is_default),
            sort_order, now, now,
        ),
    )
