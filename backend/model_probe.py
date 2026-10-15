"""首启模型自动探测 — OpenAI 兼容服务 /models 端点探测（独立叶子模块）。

从 database.py 原样搬出（ARCH-002 环解除）：本函数依赖 netconfig
（new_sync_client 接代理、redact_* 做凭据脱敏），而 netconfig 启动回读
HTTP 策略时又要 import database —— 保留在 database 就形成
database↔netconfig 双向懒导入环。搬到这里后 database 不再直连 netconfig；
本模块对 netconfig 保持函数内惰性导入（与原实现一致，探测不发生在导入期）。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _probe_first_model(base_url: str, api_key: str = "EMPTY",
                       timeout: float = 3.0) -> Optional[str]:
    """探测 OpenAI 兼容服务 /models 端点的第一个模型名。

    供首启模型自动种子使用：任何失败（不可达/超时/4xx/空列表/坏响应）
    都返回 None 并只写 warning 日志，静默降级为"不种子"，绝不阻塞启动。
    表仍为空时，下次 init_db（应用重启）会自动重试探测。

    S10-10 修复：必须带 `Authorization`。种子路径自己就握着
    `settings.vllm_api_key`（:889 读出来只用于写库），而开了 `--api-key` 的
    vLLM 对 /models 回 401 ⇒ 原实现 raise_for_status 抛 ⇒ 静默不种子，
    用户现象是「两个环境变量都设了模型列表还是空」。与 test_model_connection
    （routes/settings.py:196）的既有做法对齐。

    S10-09 修复：超时预算走 per-request 参数，不再当 `default_timeout` 传。
    netconfig.http_timeout 的语义是「用户设了 http.timeout_seconds 就**替换**
    调用点默认」，且 read 另有 60s 下限 —— 把 3.0 放 default_timeout 位置，等于
    启动关键路径上的探测被用户的 LLM 读超时（设计给一次生成的间隔，与探测无关）
    接管：设 600 ⇒ 单条探测 read=600s，两条 1200s。spec 写的是 3s/端点、
    总预算 ≤6s、绝不阻塞启动，per-request 值优先级高于 client 默认，故现在成立。
    """
    try:
        import netconfig
        url = base_url.rstrip("/")
        if not url.endswith("/models"):
            url = f"{url}/models"
        # 代理仍由 new_sync_client 统一接（不能丢），只把超时改成请求级
        with netconfig.new_sync_client() as _c:
            resp = _c.get(url, timeout=timeout,
                          headers={"Authorization": f"Bearer {api_key or 'EMPTY'}"})
        resp.raise_for_status()
        items = (resp.json() or {}).get("data") or []
        if items and items[0].get("id"):
            return str(items[0]["id"])
        # S10-03：base_url 可能内嵌凭据（企业网关文档形态 https://user:token@gw/），
        # 日志只留 scheme://host:port —— 与 proxy URL 同一条规矩。
        logger.warning("模型自动探测: /models 返回空列表 %s", netconfig.redact_proxy(url))
    except Exception as e:
        # 降级路径本身不许抛（docstring 的"绝不阻塞启动"）：url 已在 try 顶部
        # 拼好，这里只复用，不再 import 或解析。redact 失败就退回占位串，
        # 宁缺一条 URL 也不裸写可能带凭据的原值。
        # ⚠️ 异常消息本身也会内嵌请求 URL（httpx 的 ConnectError 就写
        # "connecting to https://user:pw@gw/..."），只打码 shown 是不够的。
        try:
            shown = netconfig.redact_proxy(url)
            reason = netconfig.redact_urls_in_text(str(e))
        except Exception:
            shown, reason = "<redacted>", type(e).__name__
        logger.warning(
            "模型自动探测失败 %s: %s（未种子模型，可在设置中手动配置；"
            "服务启动后重启应用将自动重试）", shown, reason,
        )
    return None
