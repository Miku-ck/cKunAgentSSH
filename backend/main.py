"""
cKunAgentic Python 后端入口
启动: python main.py --port 7777
"""
import argparse
import asyncio
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import logconfig
from config import settings
from database import init_db
from database import seed_models_async
from routes.chat import router as chat_router
from routes.bot import router as bot_router
from routes.bot_channels import router as bot_channels_router
from routes.settings import router as settings_router
from routes.skills import router as skills_router
from routes.agent import router as agent_router
from routes.terminal import router as terminal_router
from routes.audit import router as audit_router
from routes.schedules import router as schedules_router
from routes.workflow import router as workflow_router
from routes.rollbacks import router as rollbacks_router
from routes.runbooks import router as runbooks_router
from agent.consciousness_tick import get_global_tick
from agent.audit import AuditWriteError
from agent.bridge_relay import bridge_ws_endpoint
import win_asyncio_teardown

# ── 启动耗时打点 ─────────────────────────────────────────────────────────────
# 基线 = 依赖 import 完成时刻。本文件能改的正是「import 之后 → 健康就绪」这段
# 初始化（init_db / 配置回填 / 技能同步 / 模型探测 / 渠道恢复）；解释器启动与依赖
# 导入成本用 `python -X importtime -c "import main"` 或 Electron 侧 perfMark 观测
# （导入图不倒退由 tests/test_startup_critical_path.py 守卫）。
_STARTUP_T0 = time.perf_counter()

logger = logging.getLogger(__name__)

_PERF_VERBOSE = os.environ.get("CKUN_PERF", "") == "1"


def _perf_mark(label: str) -> None:
    if _PERF_VERBOSE:
        # 用 WARNING：lifespan 的 logconfig.load_from_db 会把根级别回填成用户配置
        # （默认 warning），INFO 打点会被自己吃掉（实测 batch1/batch2/就绪三条全丢）
        logger.warning("[perf] %s @%.0fms", label, (time.perf_counter() - _STARTUP_T0) * 1000)


# ── S-B1①：入口最早安装 root handler ─────────────────────────────────────────
# 必须先于下方模块级 token 警告与 __main__ 里的启动横幅执行：此前前者经
# lastResort 裸打（无时间戳无级别）、后者是 root 默认 WARNING 丢 INFO 的死日志。
# 安装后二者按统一 `%(asctime)s %(levelname)-7s %(name)s` 格式落地，且出口
# 脱敏（SecretRedactionFilter）随 handler 同步生效 —— 早期日志没有脱敏真空期。
# 启动窗口用 info 级（横幅与初始化进度必须落地）；用户配置级别由 lifespan
# 的 logconfig.load_from_db 回填接管，运行期默认仍是 warning。
logconfig.install_early()

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


async def _restore_channels_quietly() -> None:
    """后台恢复机器人渠道（QQ 登录等纯网络动作）。

    该动作不进健康就绪关键路径（实测 172ms，网络差时更久），失败也不影响
    Agent/终端主链路，只警告。
    """
    from bot.channel_runtime import get_channel_runtime

    try:
        await get_channel_runtime().restore()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("恢复 cKunBot 机器人渠道失败: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化数据库 + 启动后台意识 tick"""
    _perf_mark("lifespan entered")

    # 模块顶层导入：原 lifespan 内联 import 每次调用重复解析模块路径查找
    import logconfig
    import netconfig
    import builtin_skills as _bs
    import database as _db
    from post_processor import init_post_process_manager, load_post_process_config_from_db

    init_db(settings.db_path)
    _perf_mark("init_db done")
    logger.info("[cKunAgentic] Database initialized: %s", settings.db_path)

    # G4/G5/G9：出站 HTTP 策略与日志级别回填策略 与 内置运维技能同步 三者互不依赖，
    # 并行执行：logconfig/netconfig 是 DB 读（决定后续 seed_models_async 的 HTTP 出站策略），
    # 技能同步是文件→DB 写。原实现三者串行，实测共 150-400ms；并行后耗时取决于最慢的那条。
    # seed_models_async（发 HTTP 探测）必须等 netconfig 完成 —— 放在下一批并行里。
    _log_cfg_t = asyncio.to_thread(logconfig.load_from_db)
    _net_cfg_t = asyncio.to_thread(netconfig.load_policy_from_db)
    _skills_t = asyncio.to_thread(_bs.sync_packaged_skills)
    for _r in await asyncio.gather(_log_cfg_t, _net_cfg_t, _skills_t, return_exceptions=True):
        if isinstance(_r, Exception):
            logger.warning("[cKunAgentic] 启动初始化（log/net 配置 / 技能同步）异常: %s", _r)
    logger.debug("[cKunAgentic] log/net config + packaged skills sync completed")
    _perf_mark("batch1 (log/net/skills) done")

    # 上一批完成后：seed_models_async 打 /models 需要 netconfig 出站策略已就绪。
    # 以下四个任务互不依赖，全部并行：
    #   - seed_models_async: HTTP 探测可用模型
    #   - reset_stale_in_progress_conversations: DB 写（崩溃恢复）
    #   - load_post_process_config_from_db: DB 读
    #   - permission_policy（allowlist TTL / auto_approve / fallback 回填）：DB 读 + 写模块值
    async def _seed_models():
        await seed_models_async(settings.db_path)

    async def _reset_stale():
        _n = await asyncio.to_thread(_db.reset_stale_in_progress_conversations)
        if _n:
            logger.info("[cKunAgentic] Reset %s stale in_progress conversation(s)", _n)

    def _load_post_config():
        post_config = load_post_process_config_from_db()
        init_post_process_manager(post_config)

    def _load_permission_policy():
        # E3：allowlist 保留时长回填。必须在任何 PermissionGate 单例创建前写入模块值，
        # 否则首次加载 allowlist 时按 TTL=0 判定，过期条目要等到下次重启才不会被撤销。
        # 审批策略另两字段同段回填：auto_approve 的 DB 开关（env kill switch 在调用处
        # 仍优先）；兜底时长只在 DB 有显式行时才推 —— 从未在设置面板改过的用户，
        # CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 的 env 覆盖保持有效。
        from agent.harness.reins.auto_approve import set_db_enabled
        from agent.harness.reins.core import set_approval_fallback_seconds
        from agent.harness.reins.gate import set_allowlist_ttl_hours
        _pol = _db.get_permission_policy()
        set_allowlist_ttl_hours(_pol["allowlist_ttl_hours"])
        set_db_enabled(_pol["auto_approve_enabled"])
        # 「用户写过」要用带区间的判定，不是「这一行存在」：手工改成 "abc" 或
        # 越界值的脏行，get_permission_policy 会回默认 600 读数，若仍按"写给"推给
        # 运行时，就把运维的 CKUNAGENTIC_PERMISSION_APPROVAL_FALLBACK_S 永久锁掉了
        # —— 与 netconfig 的 retry/timeout env 兜底同一教训（database.get_http_policy
        # 的 *_set 就是为此而存在）。
        _fallback_set = _db.is_int_app_setting_set(
            "permission.approval_fallback_s", 600, low=0, high=86400)[1]
        if _fallback_set:
            set_approval_fallback_seconds(_pol["approval_fallback_s"])

    for _r in await asyncio.gather(
        _seed_models(),
        _reset_stale(),
        asyncio.to_thread(_load_post_config),
        asyncio.to_thread(_load_permission_policy),
        return_exceptions=True,
    ):
        if isinstance(_r, Exception):
            logger.warning("[cKunAgentic] 启动初始化（模型探测 / 会话复位 / 后处理 / 审批策略）异常: %s", _r)
    _perf_mark("batch2 (seed/reset/post/policy) done")

    # 意识 tick 延迟启动：仅首次 Agent 运行时触发（ensure_started），
    # 避免纯聊天用户产生空 DB 查询开销
    tick = get_global_tick()

    # 定时任务调度引擎：懒启动，仅在存在定时任务时 begin（ensure_started）
    from agent.schedule_engine import get_schedule_engine
    schedule_engine = get_schedule_engine()
    schedule_engine.ensure_started()

    # 陪聊主动性调度（P3）：60s 轮询，无人格开启时 tick 是一次空查询，
    # 开关在人格上（proactive_enabled，默认关），这里常驻无副作用。
    from bot.proactive import get_scheduler
    proactive_scheduler = get_scheduler()
    proactive_scheduler.ensure_started()

    # Bridge WS relay（阶段 1）：Electron 主进程反向连接 /ws/bridge 的命令通道。
    # 创建即注册到模块全局（bridge_ws_endpoint 经 get_bridge_relay() 获取）。
    from agent.bridge_relay import BridgeRelay, set_bridge_relay
    set_bridge_relay(BridgeRelay())

    # 机器人渠道恢复（QQ 登录等纯网络动作）移出健康就绪关键路径：实测 172ms，
    # 网络差时更久，且它失败不影响 Agent/终端主链路。后台任务 + 退出前取消等待，
    # 保证 stop_all 不会与仍在进行的 restore 交叉。
    restore_task = asyncio.create_task(_restore_channels_quietly())

    _perf_mark("ready (health 可服务)")
    _ready_ms = (time.perf_counter() - _STARTUP_T0) * 1000
    logger.info("[cKunAgentic] 后端就绪，import 后初始化耗时 %.0fms", _ready_ms)

    yield

    if not restore_task.done():
        restore_task.cancel()
        with suppress(asyncio.CancelledError):
            await restore_task
    from bot.channel_runtime import get_channel_runtime
    await get_channel_runtime().stop_all()

    # 优雅停止后台 tick（若曾启动）
    await tick.stop()

    # 停止定时任务调度引擎
    from agent.schedule_engine import get_schedule_engine
    await get_schedule_engine().stop()

    # 停止陪聊主动性调度（P3）
    await proactive_scheduler.stop()

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

# CORS — 显式来源白名单。调用方来源集合只有两种形态：
#   dev 态：vite dev server（electron/main.ts loadURL(ELECTRON_RENDERER_URL)），
#           Origin = http://localhost:5555 / http://127.0.0.1:5555，浏览器经局域网
#           IP 访问时为 http://<私有IP>:5555；
#   打包态：loadFile(...) 的 file:// renderer，其跨源请求 Origin 头为字面量 "null"，
#           必须显式列入否则 Chromium 拦响应致前端显示"连接失败"。
# （旧正则里的 ^app://\.$ 是未落地的自定义协议计划残留 —— 全仓没有任何
#   protocol.register/handle 注册 app://，去掉以免未来真出现 app:// 伪装来源时被放行。）
app.add_middleware(
    CORSMiddleware,
    # 仅放行 dev server 来源：localhost/127.0.0.1 + 局域网私有 IP（10./172.16-31./192.168.）
    # 不用 `.*` 通配，避免任意公网主机:5555 被误放行
    allow_origin_regex=(
        r"^https?://(localhost|127\.0\.0\.1|"
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"):5555$|^null$"
    ),
    # 凭据恒不放开：本后端没有任何 Cookie 会话（鉴权走每次请求显式带的
    # X-API-Token 头，见下方 api_token_auth），前端 fetch 也从不带 credentials；
    # 放开等于给沙箱 iframe 的 Origin: null 来源（同样命中上面的 ^null$）回显
    # Access-Control-Allow-Credentials，纯增攻击面。
    allow_credentials=False,
    # PUT 必须在列：QQ 接入的 PUT /bot/qq/config 与各 /settings PUT 都靠它。曾漏配
    # PUT → 预检被 Starlette 判为 Disallowed CORS method 回 400，浏览器拦截实请求，
    # fetch 抛 Failed to fetch → 前端兜底成误导性的「网络连接失败，请检查服务是否运行」。
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # 审查外热修：渲染进程直连请求都带 X-API-Token 头（client.ts withApiToken），
    # 必须列入允许头 — Starlette ≥0.41 的 CORSMiddleware 对预检中出现未允许头的
    # OPTIONS 直接回 400 "Disallowed CORS headers"（旧版会放行），导致浏览器
    # 预检失败、所有跨源请求被拦，日志里 OPTIONS 400 刷屏即此因
    allow_headers=["Content-Type", "Authorization", "Accept", "X-API-Token"],
)

# 历史补偿中间件（Origin 为 null 时剥 Set-Cookie）已随 allow_credentials=False 一并
# 移除：它的前提是「null 来源 + 凭据放行 + 响应可能带 Cookie」。本后端从不种 Cookie，
# 且不放开凭据时浏览器对跨站 fetch/XHR 响应里的 Set-Cookie 本就按 SameSite 默认
# （Lax）丢弃 —— 补偿逻辑成了每请求空转的死代码，留着反而暗示「这里有 Cookie 在飞」。


# ── API Token 认证中间件 ─────────────────────────────────────────────────────
# 受保护范围：所有 /api/* 路由（含 SSE：POST /api/agent/run）
# 例外：GET /api/health 不认证；OPTIONS（CORS 预检）放行
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
    # 只放行 /api/health：上一行已对非 /api 前缀直接放行，/health 从未注册过，
    # 写在这里是永不命中的死分支（会让人误以为存在第二个免鉴权端点）。
    if request.method == "GET" and path == "/api/health":
        return await call_next(request)
    token = request.headers.get("X-API-Token", "")
    # 审查 MINOR 修复：compare_digest 的 str 版本要求纯 ASCII，畸形/非 ASCII 头
    # 原实现抛 TypeError → 500；两侧编码为 bytes 后恒定时间比较恒安全
    if not hmac.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8")):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.get("/api/health")
async def health():
    """健康检查 — BackendManager 轮询此端点（仅判 200）。

    唯一免 token 端点，不回显版本号：本机其他进程探测到开放端口后可借此
    指纹识别应用；版本细节由需认证的 /settings/diagnostics 提供。"""
    return {"status": "ok"}


# 审计链不可写 → 关键决策（授权生效）必须失败。原实现里 append_record 静默吞掉
# 写失败，授权照样落地：事后取证看到一条没有「谁批准了什么」的干净链。
# 统一转 503 而不是 500 —— 这是依赖面（磁盘/锁）不可用，不是请求写错了。
@app.exception_handler(AuditWriteError)
async def _audit_write_error_handler(request: Request, exc: AuditWriteError):
    logger.error("关键审计写入失败，已拒绝该动作: %s %s err=%s",
                 request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "审计链不可写，该操作已拒绝生效（请检查磁盘与数据库锁）"},
    )


# 注册路由
app.include_router(chat_router, prefix="/api")
app.include_router(bot_router, prefix="/api")
app.include_router(bot_channels_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(terminal_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(workflow_router, prefix="/api")
app.include_router(rollbacks_router, prefix="/api")
app.include_router(runbooks_router, prefix="/api")

# Bridge WS 端点：刻意不挂 /api 前缀 — X-API-Token 中间件不覆盖，
# 它有自己的 hello token 鉴权（与主进程注入后端的 ELECTRON_BRIDGE_TOKEN 比对）
app.add_api_websocket_route("/ws/bridge", bridge_ws_endpoint)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cKunAgentic Backend")
    parser.add_argument("--port", type=int, default=7777, help="监听端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    logger.info("[cKunAgentic] Starting backend on %s:%s", args.host, args.port)
    # 使用 asyncio.run(Server.serve()) 代替 uvicorn.run()，
    # 避免 PowerShell 后台运行时 WatchFiles reloader 启动缓慢导致健康检查超时
    import asyncio

    # 必须早于任何 transport 创建（win32 之外自动跳过）
    win_asyncio_teardown.install()
    config = uvicorn.Config(
        # 传 app 对象而不是 "main:app" 字符串：本文件是以 __main__ 运行的，字符串入口
        # 会让 uvicorn 再 import 一次 main 模块（实测 +36ms，并多建一份 FastAPI app、
        # 多装一次 root log handler、多生成一个临时 token），而实际服务的却是第二份。
        app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
        # S-B1②：关访问日志 —— BackendManager 以 150-500ms 轮询 /api/health，
        # 每次轮询一条 access 行把启动日志刷成健康检查流水账（与 Electron 侧
        # [Python] 行缓冲 relay 配套的统一口径）。业务可观测性走 root handler，
        # uvicorn.error（启动/停止事件）仍保留在 info。
        access_log=False,
        # Bridge WS 单帧上限（显式化默认值）：远程命令输出可能较大
        ws_max_size=16 * 1024 * 1024,
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
