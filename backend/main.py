"""
cKunAgentic Python 后端入口
启动: python main.py --port 8010
"""
import argparse
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import init_db
from database import seed_models_async
from routes.chat import router as chat_router
from routes.models import router as models_router
from routes.settings import router as settings_router
from routes.skills import router as skills_router
from routes.agent import router as agent_router
from routes.terminal import router as terminal_router
from routes.audit import router as audit_router
from routes.schedules import router as schedules_router
from routes.workflow import router as workflow_router
from agent.consciousness_tick import get_global_tick
from agent.bridge_relay import bridge_ws_endpoint

logger = logging.getLogger(__name__)

# ── API Token 认证 ────────────────────────────────────────────────────────────
# 契约：Electron spawn 时注入环境变量 CKUN_API_TOKEN；
# 客户端请求需携带 HTTP 头 X-API-Token。
# 开发模式（未注入）下生成临时 token 并打印前 8 位（不打印完整 token）。
API_TOKEN = os.environ.get("CKUN_API_TOKEN", "")
if not API_TOKEN:
    API_TOKEN = secrets.token_hex(32)
    logger.warning(
        "未收到 CKUN_API_TOKEN，已生成临时 token（开发模式），token 前 8 位: %s...",
        API_TOKEN[:8],
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化数据库 + 启动后台意识 tick"""
    init_db(settings.db_path)
    logger.info("[cKunAgentic] Database initialized: %s", settings.db_path)

    # G4/G5/G9：出站 HTTP 策略与日志级别回填。必须早于任何上游请求 —— 下面的
    # seed_models_async 就会打 /models，任何 AsyncOpenAI 客户端也在此后首次建立。
    # 顺序上先日志级别：策略加载失败本身要能被记下来。
    try:
        import logconfig
        import netconfig
        logconfig.load_from_db()
        netconfig.load_policy_from_db()
    except Exception as e:
        logger.warning("[cKunAgentic] HTTP/log policy load skipped: %s", e)

    # 包内内置运维技能同步（backend/skills_builtin/*，线程池执行不阻塞事件循环）
    try:
        import asyncio as _asyncio
        import builtin_skills as _bs
        await _asyncio.to_thread(_bs.sync_packaged_skills)
    except Exception as e:
        logger.warning("[cKunAgentic] Packaged builtin skills sync skipped: %s", e)

    # 审查修复（P2 E9）：模型种子探测（httpx.get 同步阻塞）从 init_db 移至
    # 独立异步入口，放 asyncio.to_thread 线程池执行，不阻塞事件循环
    try:
        await seed_models_async(settings.db_path)
    except Exception as e:
        logger.warning("[cKunAgentic] Model seed probe skipped: %s", e)

    # 启动对账：崩溃/重启后滞留 in_progress 的会话复位为 completed。
    # Agent run 在运行中崩溃时复位逻辑丢失，会话会永久滞留 in_progress。
    # 仅启动时执行一次 — 正常运行中 in_progress 表示真有 run 进行中。
    try:
        import asyncio as _asyncio
        import database as _db
        _n = await _asyncio.to_thread(_db.reset_stale_in_progress_conversations)
        if _n:
            logger.info("[cKunAgentic] Reset %s stale in_progress conversation(s)", _n)
    except Exception as e:
        logger.warning("[cKunAgentic] Stale conversation reset skipped: %s", e)

    # 修复：从数据库加载后处理配置，避免使用默认值导致用户设置不生效
    from post_processor import init_post_process_manager, load_post_process_config_from_db
    post_config = load_post_process_config_from_db()
    init_post_process_manager(post_config)

    # E3：allowlist 保留时长回填。必须在任何 PermissionGate 单例创建前写入模块值，
    # 否则首次加载 allowlist 时按 TTL=0 判定，过期条目要等到下次重启才不会被撤销。
    # 审批策略另两字段同段回填：auto_approve 的 DB 开关（env kill switch 在调用处
    # 仍优先）；兜底时长只在 DB 有显式行时才推 —— 从未在设置面板改过的用户，
    # CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 的 env 覆盖保持有效。
    try:
        import database as _db
        from agent.harness.reins.auto_approve import set_db_enabled
        from agent.harness.reins.core import set_approval_fallback_seconds
        from agent.harness.reins.gate import set_allowlist_ttl_hours
        _pol = _db.get_permission_policy()
        set_allowlist_ttl_hours(_pol["allowlist_ttl_hours"])
        set_db_enabled(_pol["auto_approve_enabled"])
        if _db.get_app_setting("permission.approval_fallback_s") is not None:
            set_approval_fallback_seconds(_pol["approval_fallback_s"])
    except Exception as e:
        logger.warning("[cKunAgentic] Permission policy load skipped: %s", e)

    # 意识 tick 延迟启动：仅首次 Agent 运行时触发（ensure_started），
    # 避免纯聊天用户产生空 DB 查询开销
    tick = get_global_tick()

    # 定时任务调度引擎：懒启动，仅在存在定时任务时 begin（ensure_started）
    from agent.schedule_engine import get_schedule_engine
    schedule_engine = get_schedule_engine()
    schedule_engine.ensure_started()

    # Bridge WS relay（阶段 1）：Electron 主进程反向连接 /ws/bridge 的命令通道。
    # 创建即注册到模块全局（bridge_ws_endpoint 经 get_bridge_relay() 获取）。
    from agent.bridge_relay import BridgeRelay, set_bridge_relay
    set_bridge_relay(BridgeRelay())

    yield

    # 优雅停止后台 tick（若曾启动）
    await tick.stop()

    # 停止定时任务调度引擎
    from agent.schedule_engine import get_schedule_engine
    await get_schedule_engine().stop()

    # 注销 Bridge WS relay（优雅关闭时新连接将收到 1013）
    from agent.bridge_relay import set_bridge_relay
    set_bridge_relay(None)

    # 等待 PostProcessManager 任务完成（最多 10 秒），避免数据库写入被截断
    from post_processor import get_post_process_manager
    await get_post_process_manager().shutdown(timeout=10.0)


app = FastAPI(
    title="cKunAgentic Backend",
    version=settings.version,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS — 允许 Electron 渲染进程来源 + 任意主机:5173（dev server 浏览器访问，涵盖局域网 IP）
# file:// renderer 的 Origin 头为 "null"，必须显式列入否则 Chromium 拦响应致前端显示"连接失败"
# 浏览器从任意主机:5173 进入时携带不同 Origin，统一用正则匹配放行（仅 dev 环境，生产用 app://）
app.add_middleware(
    CORSMiddleware,
    # 仅放行 dev server 来源：localhost/127.0.0.1 + 局域网私有 IP（10./172.16-31./192.168.）
    # 不用 `.*` 通配，避免任意公网主机:5173 被误放行
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"):5173$|^app://\.$|^null$"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # 审查外热修：渲染进程直连请求都带 X-API-Token 头（client.ts withApiToken），
    # 必须列入允许头 — Starlette ≥0.41 的 CORSMiddleware 对预检中出现未允许头的
    # OPTIONS 直接回 400 "Disallowed CORS headers"（旧版会放行），导致浏览器
    # 预检失败、所有跨源请求被拦，日志里 OPTIONS 400 刷屏即此因
    allow_headers=["Content-Type", "Authorization", "Accept", "X-API-Token"],
)


# 修复：CORS 安全中间件 — Origin 为 null 时不允许 credentials，防止 CSRF 风险
@app.middleware("http")
async def cors_null_origin_security(request, call_next):
    """当 Origin 为 null 时，移除 Set-Cookie 头，防止跨站凭证泄露"""
    response = await call_next(request)
    origin = request.headers.get("origin")
    if origin == "null":
        # Origin 为 null 时不应设置 cookies，防止跨站请求伪造
        response.headers.pop("set-cookie", None)
    return response


# ── API Token 认证中间件 ─────────────────────────────────────────────────────
# 受保护范围：所有 /api/* 路由（含 SSE：POST /api/agent/run）
# 例外：GET /api/health（及 /health）不认证；OPTIONS（CORS 预检）放行
# 校验：X-API-Token 头与 API_TOKEN 恒定时间比较（hmac.compare_digest）
# 注意：本中间件经 @app.middleware 注册后被置于中间件栈最外层（先于 CORS 执行），
#       未授权请求直接返回 401，不经 CORS 处理——对未授权请求这是期望行为。
@app.middleware("http")
async def api_token_auth(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.method == "GET" and path in ("/api/health", "/health"):
        return await call_next(request)
    token = request.headers.get("X-API-Token", "")
    # 审查 MINOR 修复：compare_digest 的 str 版本要求纯 ASCII，畸形/非 ASCII 头
    # 原实现抛 TypeError → 500；两侧编码为 bytes 后恒定时间比较恒安全
    if not hmac.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.get("/api/health")
async def health():
    """健康检查 — BackendManager 轮询此端点"""
    return {
        "status": "ok",
        "version": app.version,
    }


# 注册路由
app.include_router(chat_router, prefix="/api")
app.include_router(models_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(terminal_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(workflow_router, prefix="/api")

# Bridge WS 端点：刻意不挂 /api 前缀 — X-API-Token 中间件不覆盖，
# 它有自己的 hello token 鉴权（与主进程注入后端的 ELECTRON_BRIDGE_TOKEN 比对）
app.add_api_websocket_route("/ws/bridge", bridge_ws_endpoint)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cKunAgentic Backend")
    parser.add_argument("--port", type=int, default=8010, help="监听端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    logger.info("[cKunAgentic] Starting backend on %s:%s", args.host, args.port)
    # 使用 asyncio.run(Server.serve()) 代替 uvicorn.run()，
    # 避免 PowerShell 后台运行时 WatchFiles reloader 启动缓慢导致健康检查超时
    import asyncio
    config = uvicorn.Config(
        "main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
        # Bridge WS 单帧上限（显式化默认值）：远程命令输出可能较大
        ws_max_size=16 * 1024 * 1024,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
